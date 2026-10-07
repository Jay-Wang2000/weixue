"""Proactive messages: an authenticated webhook queues an announcement or a conversation
opener for a device; the device takes it over its open session or, when idle, through the
long-poll that reserves a message and makes it open a session (docs/proactive.md)."""
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

import weixue_patches
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
# Delay before attempt n+1 after attempt n failed before any audio.
RETRY_BACKOFF_SECONDS = (2, 5)
KEY_RETENTION_SECONDS = 86400
CONTENT_DEDUP_SECONDS = 60
RATE_BURST = 5
RATE_PER_SECOND = 0.1
POLL_HOLD_SECONDS = 25
POLL_SEEN_SECONDS = 60
RESERVE_SECONDS = 15
PROBE_TIMEOUT_SECONDS = 3
MAX_HELD_POLLS = 8
MAX_DEVICES = 32
SESSION_QUIET_SECONDS = 1.5
SESSION_SETTLE_SECONDS = 3
TTS_READY_SECONDS = 5
SPEECH_TIMEOUT_SECONDS = 90
REPLY_WINDOW_SECONDS = 8
REPLY_ACTIVITY_SECONDS = 4
REPLY_MAX_SECONDS = 20
DISPATCH_INTERVAL_SECONDS = 0.5

FINAL = ("delivered", "answered", "no_reply", "partial", "expired", "failed")
# Speaker outcomes after which the message counts as heard.
HEARD = ("spoken", "interrupted")
# Speaker outcomes that finish the message as partial, with their error.
PARTIAL = {
    "partial": "session lost after audio started",
    "incomplete": "synthesis failed after audio started",
}


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
    reserved_for: Optional[str] = None
    reserved_until: float = 0.0
    lease: Optional[str] = None
    leased_at: float = 0.0
    claim: Optional[str] = None
    retry_at: float = 0.0

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


class DeviceAuth:
    """The server.auth boundary of the WebSocket server (upstream _handle_auth plus
    weixue_patches.refuse_unlisted_websocket), for device requests: with auth enabled, a Device-Id
    on allowed_devices; with allowed_devices empty, a Bearer token that AuthManager issued for the
    request's Client-Id and Device-Id. With auth disabled no device is verified."""

    def __init__(self, enabled, allowed_devices=(), verify_token=None):
        self.auth_enable = bool(enabled)
        self.allowed_devices = set(allowed_devices or ())
        self.verify_token = verify_token

    @classmethod
    def from_config(cls, config):
        from core.auth import AuthManager

        server = config.get("server") or {}
        auth = server.get("auth") or {}
        manager = AuthManager(secret_key=server.get("auth_key") or "", expire_seconds=auth.get("expire_seconds"))
        return cls(auth.get("enabled", False), auth.get("allowed_devices"), manager.verify_token)

    def device(self, headers):
        """The verified device id (normalized MAC) of request headers, else None."""
        if not self.auth_enable or headers is None:
            return None
        raw = headers.get("device-id")
        device = _norm_device(raw)
        if device is None or weixue_patches.unlisted(self, raw):
            return None
        if raw in self.allowed_devices:
            return device
        bearer = headers.get("authorization") or ""
        client = headers.get("client-id")
        if bearer.startswith("Bearer ") and client and self.verify_token is not None:
            if self.verify_token(bearer[7:], client_id=client, username=raw):
                return device
        return None


class Proactive:
    def __init__(self, token, clock=time.monotonic, wall=time.time, speaker=None, auth=None):
        self.token = token or ""
        self.clock = clock
        self.wall = wall
        self.speaker = speaker or speak
        self.auth = auth or DeviceAuth(False)
        self.logger = setup_logging()
        self.messages = {}
        self.keys = {}
        self.queue = deque()
        self.sessions = {}
        self.last_poll = {}
        self.polls = {}
        self.muted_until = 0.0
        self.buckets = {}
        self.reply_window = REPLY_WINDOW_SECONDS
        self.session_quiet = SESSION_QUIET_SECONDS
        self._event = asyncio.Event()
        self._dispatcher = None
        self._delivery = None

    def configure_auth(self, config):
        self.auth = DeviceAuth.from_config(config)

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

    def _reserved(self, item, now):
        return item.reserved_for is not None and now < item.reserved_until

    def expire(self):
        """Queued messages past their TTL expire, except while a poll's reservation holds them."""
        now = self.clock()
        for item in list(self.queue):
            if item.status == "queued" and now >= item.expires and not self._reserved(item, now):
                self._finish(item, "expired")
        for mid, item in list(self.messages.items()):
            if item.status in FINAL and now - item.created > KEY_RETENTION_SECONDS:
                del self.messages[mid]
                if item.key and self.keys.get(item.key) == mid:
                    del self.keys[item.key]

    def next_for(self, device, reserved=True):
        """First queued message the device may play now (mute holds all but urgent ones, a retry
        waits for its backoff). A message reserved for another device is skipped, and one reserved
        for this device only when reserved."""
        self.expire()
        muted = self.muted()
        now = self.clock()
        for item in self.queue:
            if item.status != "queued" or item.device not in (None, device) or not (item.urgent or not muted):
                continue
            if now < item.retry_at:
                continue
            if self._reserved(item, now) and (item.reserved_for != device or not reserved):
                continue
            return item
        return None

    def _release(self, item):
        item.reserved_for = None
        item.reserved_until = 0.0
        item.lease = None

    def leased(self, device, opened, message_id=None, lease=None):
        """The queued message whose unexpired lease the device holds. With message_id and lease,
        only that message under that lease; without them, a lease issued before the session
        opened (`opened`, self.clock() time)."""
        self.expire()
        now = self.clock()
        for item in self.queue:
            if item.status != "queued" or item.reserved_for != device or not self._reserved(item, now):
                continue
            if message_id is not None:
                return item if item.id == message_id and hmac.compare_digest(item.lease or "", lease or "") else None
            return item if opened is not None and item.leased_at <= opened else None
        return None

    def reserve(self, device):
        """Leases the next message the device may play to it (one lease per device: an older one
        is released) for RESERVE_SECONDS."""
        item = self.next_for(device)
        if item is not None:
            for other in self.queue:
                if other is not item and other.reserved_for == device:
                    self._release(other)
            now = self.clock()
            item.reserved_for = device
            item.reserved_until = now + RESERVE_SECONDS
            item.lease = uuid.uuid4().hex
            item.leased_at = now
            self.logger.bind(tag=TAG).info(f"proactive {item.id} reserved for {device} urgent={item.urgent}")
        return item

    def known_devices(self):
        return set(self.sessions) | set(self.last_poll)

    def live_sessions(self, device):
        """Registered sessions of the device whose connection has not stopped."""
        return [c for c in self.sessions.get(device, []) if not c.stop_event.is_set()]

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

    def on_session_open(self, device, conn, ws=None):
        """Registers a session whose device identity self.auth verified."""
        conn._weixue_ws = ws
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

    def _seen(self, device):
        self.last_poll[device] = self.clock()
        while len(self.last_poll) > MAX_DEVICES:
            del self.last_poll[min(self.last_poll, key=self.last_poll.get)]

    async def _probe(self, device):
        """Pings the device's registered sessions; one that fails to answer within
        PROBE_TIMEOUT_SECONDS is unregistered and closed."""

        async def answers(conn):
            ws = getattr(conn, "_weixue_ws", None)
            if ws is None:
                return True
            try:
                await asyncio.wait_for(await ws.ping(), PROBE_TIMEOUT_SECONDS)
                return True
            except Exception:
                return False

        conns = list(self.sessions.get(device, []))
        for conn, alive in zip(conns, await asyncio.gather(*(answers(c) for c in conns))):
            if not alive:
                self.logger.bind(tag=TAG).info(f"proactive: session {conn.session_id} of {device} did not answer a ping; closed")
                self.on_session_close(device, conn)
                asyncio.get_running_loop().create_task(_close_quietly(conn))

    async def poll(self, device):
        """Long-poll for a verified device: the message reserved for it once one is deliverable
        and the device has no live session, else None after POLL_HOLD_SECONDS or when a newer
        poll from the device replaces this one."""
        if device not in self.polls and len(self.polls) >= MAX_HELD_POLLS:
            raise RequestError(429, "too many polls", retry_after=5)
        ticket = object()
        self.polls[device] = ticket
        self._notify()
        deadline = self.clock() + POLL_HOLD_SECONDS
        try:
            self._seen(device)
            if self.sessions.get(device):
                await self._probe(device)
            while self.polls.get(device) is ticket:
                self._seen(device)
                if not self.live_sessions(device):
                    item = self.reserve(device)
                    if item is not None:
                        return item
                remaining = deadline - self.clock()
                if remaining <= 0:
                    return None
                await self._wait_change(min(remaining, 1.0))
            return None
        finally:
            if self.polls.get(device) is ticket:
                del self.polls[device]

    def claim(self, conn, item):
        """Marks item as delivering for conn before its delivery task exists; only that claim
        may finish or requeue it. Returns the task."""
        item.status = "delivering"
        item.attempts += 1
        self._release(item)
        item.claim = uuid.uuid4().hex
        conn._weixue_busy = True
        self._delivery = asyncio.get_running_loop().create_task(self.deliver(conn, item, item.claim))
        return self._delivery

    async def on_invoke(self, conn, text=INVOKE_TEXT):
        """The device opened this session because a poll answered 200: the message leased to this
        verified device is delivered (`leased`; text INVOKE_TEXT + ":<id>:<lease>" names it).
        Anything else, including a stale or expired lease, closes the session and consumes
        nothing. Delivery runs as a task: the caller is the session's message loop, which must
        keep reading the device's audio."""
        conn._weixue_invoked = True
        conn.reset_audio_states()
        conn.last_activity_time = time.time() * 1000
        if getattr(conn, "_weixue_busy", False):
            return
        device = self.auth.device(getattr(conn, "headers", None))
        binding = parse_invoke(text)
        item = None
        if device and binding is not None:
            item = self.leased(device, getattr(conn, "_weixue_opened", None), *binding)
        if item is not None and self.muted() and not item.urgent:
            self._release(item)
            item = None
        if item is None:
            why = "no current lease for it" if device else "unverified device"
            self.logger.bind(tag=TAG).info(f"proactive session opened with {why}; closing")
            await conn.close()
            return
        self.claim(conn, item)

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
            item = self.next_for(device, reserved=False)
            if item is not None and self.quiescent(conn):
                self.claim(conn, item)

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

    def _retry_or_fail(self, item, outcome):
        """A failure before any audio: requeued after RETRY_BACKOFF_SECONDS, failed after
        MAX_ATTEMPTS, expired past its TTL."""
        if item.attempts >= MAX_ATTEMPTS:
            self._finish(item, "failed", outcome)
        elif self.clock() >= item.expires:
            self._finish(item, "expired")
        else:
            delay = RETRY_BACKOFF_SECONDS[min(item.attempts, len(RETRY_BACKOFF_SECONDS)) - 1]
            item.status = "queued"
            item.claim = None
            item.retry_at = self.clock() + delay
            self.logger.bind(tag=TAG).info(f"proactive {item.id} requeued: {outcome}; retry in {delay}s")
            self._notify()

    async def deliver(self, conn, item, claim):
        """Speaks a claimed message. Heard (spoken, or interrupted after audio) finishes it;
        audio cut off by a lost session or a failed synthesis finishes it as partial; a failure
        or an error before any audio goes to _retry_or_fail; an error after the message was heard
        finishes it as partial."""
        invoked = getattr(conn, "_weixue_invoked", False)
        self.logger.bind(tag=TAG).info(
            f"proactive {item.id} delivering: attempt={item.attempts} session={conn.session_id} "
            f"invoked={invoked} urgent={item.urgent}"
        )
        heard = False
        try:
            outcome = await self.speaker(conn, item, close_after=invoked and item.mode == "announce")
            if item.claim != claim:
                return
            if outcome in PARTIAL:
                self._finish(item, "partial", PARTIAL[outcome])
                return
            if outcome not in HEARD:
                self._retry_or_fail(item, outcome)
                return
            heard = True
            if item.mode == "announce":
                self._finish(item, "delivered")
                return
            await self._await_reply(conn, item, invoked, claim)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"proactive {item.id} delivery error: {type(e).__name__}: {e}")
            if item.claim == claim and item.status == "delivering":
                if heard:
                    self._finish(item, "partial", "error after audio started")
                else:
                    self._retry_or_fail(item, "error")
        finally:
            conn._weixue_busy = False
            self._notify()

    async def _await_reply(self, conn, item, invoked, claim):
        """Short-reply window: the first user turn after the opener is its answer."""
        start = time.time()
        anchor = getattr(conn, "_weixue_anchor", None)
        while item.claim == claim:
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


async def _close_quietly(conn):
    try:
        await conn.close()
    except Exception:
        pass


async def speak(conn, item, close_after):
    """Plays item.text as an assistant turn (conn._weixue_anchor) after a user-role note of the
    event. The outcome comes from the sentence's own playback record (weixue_patches.track_playback),
    whose frames are packets the transport wrote: with audio written, "spoken" (its LAST completed,
    weixue_patches.SentencePlayback), "incomplete" (completed but a segment's synthesis failed),
    "interrupted" (abort or a newer turn) or "partial" (a write failed, session closed, an error,
    or SPEECH_TIMEOUT_SECONDS passed); with none written, "no_audio" (synthesis produced none),
    "send_failed", "superseded", "closed", "no_tts", "timeout" or "error"."""
    from core.handle.sendAudioHandle import send_tts_message
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
    playback = weixue_patches.track_playback(conn, sentence_id)
    try:
        return await _play_tracked(conn, item, close_after, sentence_id, playback, send_tts_message)
    except Exception as e:
        setup_logging().bind(tag=TAG).warning(f"proactive {item.id} playback error: {type(e).__name__}")
        return "partial" if playback.frames > 0 else "error"
    finally:
        weixue_patches.untrack_playback(conn, sentence_id)


async def _play_tracked(conn, item, close_after, sentence_id, playback, send_tts_message):
    """speak() after the dialogue entries: queues the sentence and waits for its outcome."""
    from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO

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
    while True:
        heard = playback.frames > 0
        if playback.done.is_set():
            if not heard:
                return "no_audio"
            return "incomplete" if playback.failed else "spoken"
        if conn.client_abort or conn.sentence_id != sentence_id:
            return "interrupted" if heard else "superseded"
        if playback.send_error:
            return "partial" if heard else "send_failed"
        if conn.stop_event.is_set():
            return "partial" if heard else "closed"
        if time.time() > deadline:
            return "partial" if heard else "timeout"
        try:
            await asyncio.wait_for(playback.done.wait(), 0.1)
        except asyncio.TimeoutError:
            pass


# HTTP

def _client(request):
    return request.remote or "unknown"


def parse_invoke(text):
    """(message id, lease) of an invocation text INVOKE_TEXT + ":<id>:<lease>", (None, None) for
    INVOKE_TEXT alone, else None."""
    if text == INVOKE_TEXT:
        return None, None
    prefix = INVOKE_TEXT + ":"
    if not isinstance(text, str) or not text.startswith(prefix):
        return None
    parts = text[len(prefix):].split(":")
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0], parts[1]


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
        if not proactive.auth.auth_enable:
            return web.json_response({"error": "disabled: server.auth is not enabled"}, status=503)
        device = proactive.auth.device(request.headers)
        if device is None:
            return web.json_response({"error": "unauthorized device"}, status=401)
        try:
            item = await proactive.poll(device)
        except RequestError as e:
            return web.json_response(e.body, status=e.status, headers={"Retry-After": str(e.body["retry_after"])})
        if item is not None:
            return web.json_response({"pending": True, "urgent": item.urgent, "id": item.id, "lease": item.lease})
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
