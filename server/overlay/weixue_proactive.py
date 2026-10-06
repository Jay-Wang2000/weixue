"""Proactive messages: an authenticated webhook queues an announcement or a conversation
opener for a device; the device takes it over its open session or, when idle, through the
long-poll that makes it open one (docs/proactive.md)."""
import asyncio
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from config.logger import setup_logging

TAG = __name__
_CJK = re.compile(r"[\u3400-\u9fff]")
INVOKE_TEXT = "weixue:proactive"
TOKEN_ENV = "WEIXUE_PROACTIVE_TOKEN"

MODES = ("announce", "conversation")
MAX_MESSAGE_CHARS = 500
MAX_CONTEXT_CHARS = 1000
MAX_KEY_CHARS = 128
DEFAULT_TTL_SECONDS = 600
MIN_TTL_SECONDS = 10
MAX_TTL_SECONDS = 3600
MAX_MUTE_MINUTES = 1440
MAX_QUEUED = 20
MAX_ATTEMPTS = 3
KEY_RETENTION_SECONDS = 86400
CONTENT_DEDUP_SECONDS = 60
RATE_BURST = 5
RATE_PER_SECOND = 0.1
POLL_HOLD_SECONDS = 25
POLL_SEEN_SECONDS = 60
SESSION_QUIET_SECONDS = 1.5
SESSION_SETTLE_SECONDS = 3
TTS_READY_SECONDS = 5
SPEECH_TIMEOUT_SECONDS = 90
REPLY_WINDOW_SECONDS = 8
REPLY_ACTIVITY_SECONDS = 4
REPLY_MAX_SECONDS = 20
DISPATCH_INTERVAL_SECONDS = 0.5

FINAL = ("delivered", "answered", "no_reply", "expired", "failed")


def _iso(wall):
    return datetime.fromtimestamp(wall, timezone.utc).isoformat(timespec="seconds") if wall else None


@dataclass
class Message:
    id: str
    mode: str
    text: str
    context: str
    device: Optional[str]
    urgent: bool
    key: Optional[str]
    fingerprint: str
    created: float
    expires: float
    created_wall: float
    status: str = "queued"
    attempts: int = 0
    reply: Optional[str] = None
    finished_wall: Optional[float] = None
    error: str = ""

    def public(self, reason=""):
        out = {
            "id": self.id,
            "mode": self.mode,
            "status": self.status,
            "device": self.device,
            "attempts": self.attempts,
            "created_at": _iso(self.created_wall),
            "finished_at": _iso(self.finished_wall),
        }
        if reason:
            out["reason"] = reason
        if self.reply is not None:
            out["reply"] = self.reply
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class TokenBucket:
    capacity: float
    rate: float
    tokens: float
    stamp: float

    def take(self, now):
        """Takes one token; returns 0 on success, else the seconds until one is available."""
        self.tokens = min(self.capacity, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        return (1 - self.tokens) / self.rate


class RequestError(Exception):
    def __init__(self, status, error, **extra):
        super().__init__(error)
        self.status = status
        self.body = {"error": error, **extra}


def _fingerprint(mode, text, context, device, urgent):
    raw = json.dumps([mode, text, context, device, urgent], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _norm_device(value):
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def parse_request(body, header_key=None):
    """Validates a webhook body; returns the fields of a new Message."""
    if not isinstance(body, dict):
        raise RequestError(400, "body must be a JSON object")
    mode = body.get("mode", "announce")
    if mode not in MODES:
        raise RequestError(400, f"mode must be one of {list(MODES)}")
    text = body.get("message")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE_CHARS:
        raise RequestError(400, f"message must be a non-empty string of at most {MAX_MESSAGE_CHARS} characters")
    context = body.get("context") or ""
    if not isinstance(context, str) or len(context) > MAX_CONTEXT_CHARS:
        raise RequestError(400, f"context must be a string of at most {MAX_CONTEXT_CHARS} characters")
    ttl = body.get("ttl", DEFAULT_TTL_SECONDS)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not MIN_TTL_SECONDS <= ttl <= MAX_TTL_SECONDS:
        raise RequestError(400, f"ttl must be an integer from {MIN_TTL_SECONDS} to {MAX_TTL_SECONDS}")
    urgent = body.get("urgent", False)
    if not isinstance(urgent, bool):
        raise RequestError(400, "urgent must be a boolean")
    device = body.get("device")
    if device is not None and _norm_device(device) is None:
        raise RequestError(400, "device must be a non-empty string")
    key = header_key if header_key is not None else body.get("id")
    if key is not None and (not isinstance(key, str) or not key.strip() or len(key) > MAX_KEY_CHARS):
        raise RequestError(400, f"idempotency key must be a non-empty string of at most {MAX_KEY_CHARS} characters")
    return {
        "mode": mode,
        "text": text.strip(),
        "context": context.strip(),
        "device": _norm_device(device),
        "urgent": urgent,
        "key": key.strip() if key else None,
        "ttl": ttl,
    }


def _dialogue_text(item):
    language = "Simplified Chinese" if _CJK.search(item.text) else "English"
    lines = [f"(Home automation event, not spoken by the user) You are telling the household: {item.text}"]
    if item.context:
        lines.append(f"Context: {item.context}")
    if item.mode == "conversation":
        lines.append(f"Their next utterance answers it. Reply in {language} unless they answer in another language.")
    return "\n".join(lines)


def _user_text(content):
    if isinstance(content, str) and content.strip().startswith("{"):
        try:
            data = json.loads(content)
            if isinstance(data, dict) and "content" in data:
                return str(data["content"])
        except json.JSONDecodeError:
            pass
    return content


def _reply_after(conn, anchor):
    """First real user turn after the opener's assistant entry (few-shot entries are temporary)."""
    messages = conn.dialogue.dialogue
    for i, message in enumerate(messages):
        if message is anchor:
            for later in messages[i + 1:]:
                if later.role == "user" and not getattr(later, "is_temporary", False):
                    return later
            return None
    return None


class Proactive:
    def __init__(self, token, clock=time.monotonic, wall=time.time, speaker=None):
        self.token = token or ""
        self.clock = clock
        self.wall = wall
        self.speaker = speaker or speak
        self.logger = setup_logging()
        self.messages = {}
        self.keys = {}
        self.queue = deque()
        self.sessions = {}
        self.last_poll = {}
        self.poll_started = {}
        self.muted_until = 0.0
        self.buckets = {}
        self.reply_window = REPLY_WINDOW_SECONDS
        self.session_quiet = SESSION_QUIET_SECONDS
        self._event = asyncio.Event()
        self._dispatcher = None
        self._delivery = None

    # Queue state

    def _notify(self):
        self._event.set()
        self._event = asyncio.Event()

    async def _wait_change(self, timeout):
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    def muted(self):
        return self.clock() < self.muted_until

    def set_mute(self, minutes):
        self.muted_until = self.clock() + minutes * 60 if minutes else 0.0
        self._notify()
        return self.mute_state()

    def mute_state(self):
        remaining = self.muted_until - self.clock()
        return {"muted": remaining > 0, "muted_until": _iso(self.wall() + remaining) if remaining > 0 else None}

    def _finish(self, item, status, error=""):
        item.status = status
        item.error = error
        item.finished_wall = self.wall()
        if item in self.queue:
            self.queue.remove(item)
        self.logger.bind(tag=TAG).info(f"proactive {item.id} {status}{(': ' + error) if error else ''}")
        self._notify()

    def expire(self):
        now = self.clock()
        for item in list(self.queue):
            if item.status == "queued" and now >= item.expires:
                self._finish(item, "expired")
        for mid, item in list(self.messages.items()):
            if item.status in FINAL and now - item.created > KEY_RETENTION_SECONDS:
                del self.messages[mid]
                if item.key and self.keys.get(item.key) == mid:
                    del self.keys[item.key]

    def next_for(self, device):
        """First queued message the device may play now (mute holds all but urgent ones)."""
        self.expire()
        muted = self.muted()
        for item in self.queue:
            if item.status == "queued" and item.device in (None, device) and (item.urgent or not muted):
                return item
        return None

    def known_devices(self):
        return set(self.sessions) | set(self.last_poll)

    def live_sessions(self, device):
        """Sessions opened after the device's latest poll; the device polls only while idle, so older ones are stale."""
        since = self.poll_started.get(device, float("-inf"))
        return [c for c in self.sessions.get(device, []) if c._weixue_opened >= since]

    def reachable(self, device):
        if device is None:
            return any(self.reachable(d) for d in self.known_devices())
        seen = self.last_poll.get(device)
        return bool(self.live_sessions(device)) or (seen is not None and self.clock() - seen < POLL_SEEN_SECONDS)

    def reason(self, item):
        if item.status != "queued":
            return ""
        if self.muted() and not item.urgent:
            return "muted"
        if not self.reachable(item.device):
            return "offline"
        devices = [item.device] if item.device else list(self.known_devices())
        if devices and all(self.live_sessions(d) for d in devices):
            return "busy"
        return ""

    def submit(self, fields, client):
        """Applies dedup and rate limit, then queues; returns (http status, body)."""
        self.expire()
        now = self.clock()
        fingerprint = _fingerprint(fields["mode"], fields["text"], fields["context"], fields["device"], fields["urgent"])
        if fields["key"]:
            existing = self.messages.get(self.keys.get(fields["key"]))
            if existing:
                if existing.fingerprint != fingerprint:
                    raise RequestError(409, "idempotency key reused with a different request", id=existing.id)
                return 200, {**existing.public(self.reason(existing)), "duplicate": True}
        else:
            for existing in self.messages.values():
                if (
                    existing.key is None
                    and existing.fingerprint == fingerprint
                    and now - existing.created < CONTENT_DEDUP_SECONDS
                    and existing.status not in ("expired", "failed")
                ):
                    return 200, {**existing.public(self.reason(existing)), "duplicate": True}

        bucket = self.buckets.setdefault(client, TokenBucket(RATE_BURST, RATE_PER_SECOND, RATE_BURST, now))
        wait = bucket.take(now)
        if wait:
            raise RequestError(429, "rate limited", retry_after=int(wait) + 1)
        if len(self.queue) >= MAX_QUEUED:
            raise RequestError(429, "queue full", retry_after=30)

        item = Message(
            id=uuid.uuid4().hex,
            mode=fields["mode"],
            text=fields["text"],
            context=fields["context"],
            device=fields["device"],
            urgent=fields["urgent"],
            key=fields["key"],
            fingerprint=fingerprint,
            created=now,
            expires=now + fields["ttl"],
            created_wall=self.wall(),
        )
        self.messages[item.id] = item
        if item.key:
            self.keys[item.key] = item.id
        self.queue.append(item)
        self.logger.bind(tag=TAG).info(
            f"proactive {item.id} queued: mode={item.mode} device={item.device or 'any'} urgent={item.urgent} ttl={fields['ttl']}"
        )
        self._notify()
        return 202, item.public(self.reason(item))

    def summary(self):
        self.expire()
        devices = {}
        for device in sorted(self.known_devices()):
            seen = self.last_poll.get(device)
            devices[device] = {
                "session_open": bool(self.live_sessions(device)),
                "last_poll_seconds_ago": round(self.clock() - seen, 1) if seen is not None else None,
                "reachable": self.reachable(device),
            }
        return {
            "devices": devices,
            **self.mute_state(),
            "queued": [m.public(self.reason(m)) for m in self.queue],
        }

    # Device side

    def on_session_open(self, device, conn):
        conn._weixue_opened = self.clock()
        self.sessions.setdefault(device, []).append(conn)
        self._notify()

    def on_session_close(self, device, conn):
        conns = self.sessions.get(device, [])
        if conn in conns:
            conns.remove(conn)
        if not conns:
            self.sessions.pop(device, None)
        self._notify()

    async def poll(self, device):
        """Long-poll: True once a message is deliverable and the device has no open session."""
        started = self.clock()
        self.poll_started[device] = started
        deadline = started + POLL_HOLD_SECONDS
        while True:
            self.last_poll[device] = self.clock()
            if not self.live_sessions(device) and self.next_for(device):
                return True
            remaining = deadline - self.clock()
            if remaining <= 0:
                return False
            await self._wait_change(min(remaining, 1.0))

    async def on_invoke(self, conn):
        """The device opened this session because a poll answered 200. Delivery runs as a task:
        the caller is the session's message loop, which must keep reading the device's audio."""
        conn._weixue_invoked = True
        conn.reset_audio_states()
        conn.last_activity_time = time.time() * 1000
        if getattr(conn, "_weixue_busy", False):
            return
        device = _norm_device(getattr(conn, "device_id", None))
        item = self.next_for(device)
        if item is None:
            self.logger.bind(tag=TAG).info("proactive session opened with nothing to deliver; closing")
            await conn.close()
            return
        conn._weixue_busy = True
        self._delivery = asyncio.get_running_loop().create_task(self.deliver(conn, item))

    def quiescent(self, conn):
        if getattr(conn, "_weixue_busy", False) or conn.stop_event.is_set() or conn.tts is None:
            return False
        if conn.client_is_speaking or conn.client_have_voice:
            return False
        now_ms = time.time() * 1000
        return (
            now_ms - conn.first_activity_time >= SESSION_SETTLE_SECONDS * 1000
            and now_ms - conn.last_activity_time >= self.session_quiet * 1000
        )

    def dispatch_once(self):
        for device in list(self.sessions):
            conns = self.live_sessions(device)
            if not conns:
                continue
            conn = conns[-1]
            item = self.next_for(device)
            if item is not None and self.quiescent(conn):
                conn._weixue_busy = True
                self._delivery = asyncio.get_running_loop().create_task(self.deliver(conn, item))

    async def run(self):
        while True:
            try:
                self.dispatch_once()
            except Exception as e:
                self.logger.bind(tag=TAG).error(f"proactive dispatch failed: {e}")
            await self._wait_change(DISPATCH_INTERVAL_SECONDS)

    def start(self):
        if self._dispatcher is None or self._dispatcher.done():
            self._dispatcher = asyncio.get_running_loop().create_task(self.run())

    async def deliver(self, conn, item):
        item.status = "delivering"
        item.attempts += 1
        invoked = getattr(conn, "_weixue_invoked", False)
        self.logger.bind(tag=TAG).info(
            f"proactive {item.id} delivering: attempt={item.attempts} session={conn.session_id} invoked={invoked}"
        )
        try:
            outcome = await self.speaker(conn, item, close_after=invoked and item.mode == "announce")
            if outcome != "spoken":
                if item.attempts >= MAX_ATTEMPTS:
                    self._finish(item, "failed", outcome)
                elif self.clock() >= item.expires:
                    self._finish(item, "expired")
                else:
                    item.status = "queued"
                    self.logger.bind(tag=TAG).info(f"proactive {item.id} requeued: {outcome}")
                    self._notify()
                return
            if item.mode == "announce":
                self._finish(item, "delivered")
                return
            await self._await_reply(conn, item, invoked)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"proactive {item.id} delivery error: {e}")
            if item.status == "delivering":
                self._finish(item, "failed", "error")
        finally:
            conn._weixue_busy = False
            self._notify()

    async def _await_reply(self, conn, item, invoked):
        """Short-reply window: the first user turn after the opener is its answer."""
        start = time.time()
        anchor = getattr(conn, "_weixue_anchor", None)
        while True:
            reply = _reply_after(conn, anchor)
            if reply is not None:
                item.reply = _user_text(reply.content)
                self._finish(item, "answered")
                return
            if conn.stop_event.is_set():
                self._finish(item, "no_reply")
                return
            now = time.time()
            recent = now - conn.last_activity_time / 1000 < REPLY_ACTIVITY_SECONDS
            in_turn = conn.client_have_voice or conn.client_is_speaking or recent
            if now - start >= self.reply_window and (not in_turn or now - start >= REPLY_MAX_SECONDS):
                self._finish(item, "no_reply")
                if invoked:
                    await conn.close()
                return
            await asyncio.sleep(0.1)


async def speak(conn, item, close_after):
    """Plays item.text as an assistant turn (conn._weixue_anchor) after a user-role note of the event;
    returns "spoken", "closed", "no_tts" or "timeout"."""
    from core.handle.sendAudioHandle import send_tts_message
    from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
    from core.utils.dialogue import Message as DialogueMessage

    deadline = time.time() + TTS_READY_SECONDS
    while conn.tts is None and time.time() < deadline and not conn.stop_event.is_set():
        await asyncio.sleep(0.1)
    if conn.stop_event.is_set():
        return "closed"
    if conn.tts is None:
        return "no_tts"

    conn.dialogue.put(DialogueMessage(role="user", content=_dialogue_text(item)))
    conn._weixue_anchor = DialogueMessage(role="assistant", content=item.text)
    conn.dialogue.put(conn._weixue_anchor)
    sentence_id = uuid.uuid4().hex
    conn.client_abort = False
    conn.close_after_chat = close_after
    conn.sentence_id = sentence_id
    conn.client_is_speaking = True
    await send_tts_message(conn, "start")
    conn.tts.store_tts_text(sentence_id, item.text)
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(sentence_id=sentence_id, sentence_type=SentenceType.FIRST, content_type=ContentType.ACTION)
    )
    conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=item.text, sentence_id=sentence_id)
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(sentence_id=sentence_id, sentence_type=SentenceType.LAST, content_type=ContentType.ACTION)
    )
    deadline = time.time() + SPEECH_TIMEOUT_SECONDS
    while conn.client_is_speaking:
        if conn.stop_event.is_set():
            return "closed"
        if time.time() > deadline:
            return "timeout"
        await asyncio.sleep(0.1)
    return "spoken"


# HTTP

def _client(request):
    return request.remote or "unknown"


def _authorized(proactive, request):
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:].encode(), proactive.token.encode())


def add_routes(app, proactive):
    from aiohttp import web

    def guarded(handler):
        async def wrapper(request):
            if not proactive.token:
                return web.json_response({"error": f"disabled: {TOKEN_ENV} is not set"}, status=503)
            if not _authorized(proactive, request):
                return web.json_response({"error": "unauthorized"}, status=401)
            try:
                return await handler(request)
            except RequestError as e:
                headers = {"Retry-After": str(e.body["retry_after"])} if "retry_after" in e.body else None
                return web.json_response(e.body, status=e.status, headers=headers)

        return wrapper

    async def read_json(request):
        try:
            return await request.json()
        except ValueError:
            raise RequestError(400, "body must be JSON")

    async def create(request):
        fields = parse_request(await read_json(request), request.headers.get("Idempotency-Key"))
        status, body = proactive.submit(fields, _client(request))
        return web.json_response(body, status=status)

    async def get_message(request):
        item = proactive.messages.get(request.match_info["id"])
        if item is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(item.public(proactive.reason(item)))

    async def status(_request):
        return web.json_response(proactive.summary())

    async def mute(request):
        body = await read_json(request)
        minutes = body.get("minutes") if isinstance(body, dict) else None
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 <= minutes <= MAX_MUTE_MINUTES:
            raise RequestError(400, f"minutes must be an integer from 0 to {MAX_MUTE_MINUTES}")
        return web.json_response(proactive.set_mute(minutes))

    async def poll(request):
        device = _norm_device(request.headers.get("Device-Id"))
        if device is None:
            return web.json_response({"error": "Device-Id header required"}, status=400)
        if await proactive.poll(device):
            return web.json_response({"pending": True})
        return web.Response(status=204)

    async def start_dispatcher(_app):
        proactive.start()

    app.on_startup.append(start_dispatcher)
    app.add_routes(
        [
            web.get("/weixue/proactive/poll", poll),
            web.get("/weixue/proactive", guarded(status)),
            web.post("/weixue/proactive/messages", guarded(create)),
            web.get("/weixue/proactive/messages/{id}", guarded(get_message)),
            web.put("/weixue/proactive/mute", guarded(mute)),
        ]
    )


INSTANCE: Optional[Proactive] = None


def instance():
    global INSTANCE
    if INSTANCE is None:
        INSTANCE = Proactive(os.environ.get(TOKEN_ENV, ""))
    return INSTANCE
