"""LLM provider `claude_agent` (docs/server.md, LLM: agent-service gateway): each voice turn is one message of
the device's current agent-service chat; its reply text streams back over SSE into xiaozhi's TTS path.

response() runs in chat()'s executor thread and reads a queue that an AgentTurn coroutine fills on the
connection's event loop. AgentTurn.cancel() (abort hook, newer turn, session end) closes the HTTP stream at
once from any thread.
"""
import asyncio
import importlib
import json
import queue
import re
import threading
import time
import uuid
from collections import deque
from urllib.parse import quote

import weixue_device_tools
from config.logger import setup_logging
from core.providers.llm.base import LLMProviderBase
from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO

TAG = __name__
logger = setup_logging()

DEVICE_ACTION_TOOLS = weixue_device_tools.AGENT_VISIBLE_DEVICE_TOOLS
FALLBACK = {"en": "I can't reach my assistant right now.", "zh": "我暂时连不上助手。"}
LOST = {"en": "Sorry, I lost the connection.", "zh": "抱歉，连接断了。"}

CONNECT_TIMEOUT_S = 2
# Three missed heartbeats (agent-service sends one every 2 s while a reply is open).
STALL_S = 6
RETRY_DELAY_S = 0.3
SWITCH_WAIT_S = 1
BREAKER_FAILURES = 3
BREAKER_WINDOW_S = 60
BREAKER_OPEN_S = 30
# Upper bound between two queue items of a turn.
QUEUE_WAIT_S = 30
DEVICE_CALL_TIMEOUT_S = 6
DEVICE_RESULT_MAX = 2000
REPORT_WAIT_S = 120
SPEECH_END_MAX_AGE_S = 60

_CJK = re.compile(r"[㐀-鿿]")
_LATIN = re.compile(r"[A-Za-z]")
_TOKEN = re.compile(r"[0-9A-Za-z]+|[㐀-鿿]")
_ACTION_ID = re.compile(r"^dac_[0-9A-HJKMNP-TV-Z]{26}$")
_END = object()


def _tokens(text):
    return len(_TOKEN.findall(text or ""))


def _last_user_text(dialogue):
    for message in reversed(dialogue or []):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                return " ".join(b.get("text", "") for b in content if isinstance(b, dict)).strip()
    return ""


def turn_language(conn, text):
    """en | zh from the STT language of the utterance, else from the text; auto when neither tells."""
    stt = getattr(conn, "weixue_stt", None)
    code = (stt or {}).get("language") or ""
    code = code.lower() if isinstance(code, str) else ""
    if code.startswith(("cmn", "zh", "yue")):
        return "zh"
    if code.startswith("en"):
        return "en"
    if _CJK.search(text):
        return "zh"
    if _LATIN.search(text):
        return "en"
    return "auto"


def _spoken_lang(lang):
    return "zh" if lang == "zh" else "en"


def _ms(start, end):
    return "-" if start is None or end is None else str(max(0, round((end - start) * 1000)))


class Breaker:
    """BREAKER_FAILURES failures within BREAKER_WINDOW_S open it for BREAKER_OPEN_S; a success closes it."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.failures = deque()
        self.open_until = 0.0
        self.lock = threading.Lock()

    def allow(self):
        with self.lock:
            return not (self.open_until and self.clock() < self.open_until)

    def success(self):
        with self.lock:
            was_open = bool(self.open_until)
            self.failures.clear()
            self.open_until = 0.0
        if was_open:
            logger.bind(tag=TAG).info("claude_agent breaker closed: agent-service answered")

    def failure(self):
        now = self.clock()
        with self.lock:
            self.failures.append(now)
            while self.failures and self.failures[0] < now - BREAKER_WINDOW_S:
                self.failures.popleft()
            opening = len(self.failures) >= BREAKER_FAILURES and not (self.open_until and now < self.open_until)
            if opening:
                self.open_until = now + BREAKER_OPEN_S
        if opening:
            logger.bind(tag=TAG).warning(
                f"claude_agent breaker open for {BREAKER_OPEN_S} s after {BREAKER_FAILURES} failures "
                f"in {BREAKER_WINDOW_S} s"
            )


class TurnMetrics:
    """Timings of one turn for its `turn_latency` line. The playback hooks of weixue_patches append each
    captioned segment of the turn's sentence (text, handed_at, first written frame) to `segments`."""

    def __init__(self, sid, trace, speech_end=None, stt_ms=None, clock=time.monotonic):
        self.sid = sid
        self.trace = trace
        self.clock = clock
        self.speech_end = speech_end
        self.stt_ms = stt_ms
        self.chat = None
        self.routed = None
        self.reply_end = None
        self.post_at = None
        self.first_text_at = None
        self.reply_end_at = None
        self.text_tokens = 0
        self.answer_from = 0
        self.segments = []
        self.reported = False

    def text(self, delta):
        if self.first_text_at is None and delta.strip():
            self.first_text_at = self.clock()
        self.text_tokens += _tokens(delta)

    def tool(self):
        self.answer_from = self.text_tokens

    def line(self):
        first = self.segments[0] if self.segments else None
        first_frame = first.get("first") if first else None
        answer_frame = None
        seen = 0
        for segment in self.segments:
            seen += _tokens(segment.get("text"))
            if seen > self.answer_from:
                answer_frame = segment.get("first")
                break
        return (
            f"turn_latency trace={self.trace} chat={self.chat or '-'} routed={self.routed or '-'} "
            f"reply_end={self.reply_end or '-'} "
            f"stt_final_latency_ms={'-' if self.stt_ms is None else self.stt_ms} "
            f"service_first_text_ms={_ms(self.post_at, self.first_text_at)} "
            f"tts_first_audio_ms={_ms(first.get('handed_at') if first else None, first_frame)} "
            f"total_ms={_ms(self.speech_end, first_frame)} "
            f"answer_audio_ms={_ms(self.speech_end, answer_frame)} "
            f"reply_end_ms={_ms(self.post_at, self.reply_end_at)}"
        )

    def report(self):
        if not self.reported:
            self.reported = True
            logger.bind(tag=TAG).info(self.line())


async def report_when_played(conn, metrics, record):
    """Logs the turn's line once its sentence finished playing, went stale or REPORT_WAIT_S passed."""
    import weixue_patches

    deadline = time.monotonic() + REPORT_WAIT_S
    try:
        while time.monotonic() < deadline:
            stop = getattr(conn, "stop_event", None)
            if (
                (record is not None and record.done.is_set())
                or conn.sentence_id != metrics.sid
                or conn.client_abort
                or (stop is not None and stop.is_set())
            ):
                break
            await asyncio.sleep(0.1)
    finally:
        metrics.report()
        weixue_patches.untrack_playback(conn, metrics.sid)
        if getattr(conn, "weixue_turn_metrics", None) is metrics:
            conn.weixue_turn_metrics = None


async def run_device_tool(conn, tool, args):
    """Runs one agent-visible device MCP tool on conn's device; {"ok": bool, "result"|"error": str}."""
    if tool not in DEVICE_ACTION_TOOLS:
        logger.bind(tag=TAG).warning(f"device_action {tool!r} refused: not agent-visible")
        return {"ok": False, "error": "this device tool is not available"}
    if not isinstance(args, dict):
        return {"ok": False, "error": "args must be an object"}
    client = getattr(conn, "mcp_client", None)
    if client is None or not await client.is_ready():
        return {"ok": False, "error": "the device MCP channel is not ready"}
    name = next((s for s, real in getattr(client, "name_mapping", {}).items() if real == tool), None)
    if name is None:
        return {"ok": False, "error": "the device does not offer this tool"}
    device_mcp = importlib.import_module("core.providers.tools.device_mcp")
    try:
        raw = await device_mcp.call_mcp_tool(conn, client, name, json.dumps(args), timeout=DEVICE_CALL_TIMEOUT_S)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    return {"ok": True, "result": text[:DEVICE_RESULT_MAX]}


def _switch_pending(device_id):
    """The in-flight switch_chat / new_chat post of the device, if the agent gateway has one."""
    import sys

    delivery = sys.modules.get("weixue_agent_delivery")
    gateway = getattr(delivery, "INSTANCE", None)
    pending = getattr(getattr(gateway, "sink", None), "pending_switch", None)
    return pending(device_id) if pending is not None else None


class AgentTurn:
    """One message to agent-service and its SSE reply; items for response() go to `items`:
    ("text", delta), ("hb", None), ("speak", line) and finally _END."""

    def __init__(self, provider, conn, text, lang, seq, metrics):
        self.provider = provider
        self.conn = conn
        self.text = text
        self.lang = lang
        self.seq = seq
        self.metrics = metrics
        self.device_id = getattr(conn, "device_id", None) or (getattr(conn, "headers", None) or {}).get("device-id")
        self.items = queue.Queue()
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.future = None
        self.session = None
        self.chat_id = None
        self.turn_id = None
        self.reply_id = None
        self.accepted = False
        self.spoken = False
        self.last_seq = 0
        self.actions = set()
        # action_ids of this turn's device_action events already started (a resume may replay one).
        self.action_ids = set()

    # Any thread

    def start(self):
        self.future = asyncio.run_coroutine_threadsafe(self._run(), self.conn.loop)

    def cancel(self):
        if self.cancelled.is_set() or self.done.is_set():
            return
        self.cancelled.set()
        if self.future is not None:
            self.future.cancel()
        self.items.put(_END)

    # Event loop

    def _put(self, kind, value=None):
        if not self.cancelled.is_set():
            self.items.put((kind, value))

    async def _run(self):
        import aiohttp

        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.provider.token}"},
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=CONNECT_TIMEOUT_S, sock_read=STALL_S),
        )
        closed = False
        try:
            await self._wait_switch()
            await self._exchange()
        except asyncio.CancelledError:
            closed = True
            self.metrics.reply_end = self.metrics.reply_end or "closed"
            raise
        except Exception as e:
            logger.bind(tag=TAG).error(f"claude_agent turn error trace={self.metrics.trace}: {type(e).__name__}")
            self._failed("internal")
        finally:
            self.done.set()
            self.items.put(_END)
            await self._settle_actions(closed)
            await self.session.close()

    async def _settle_actions(self, closed):
        """Device actions still running when the reply ended post their results; a closed stream drops them."""
        if self.actions and not closed:
            try:
                await asyncio.wait(set(self.actions), timeout=DEVICE_CALL_TIMEOUT_S + 2)
            except asyncio.CancelledError:
                pass
        for task in list(self.actions):
            task.cancel()

    async def _wait_switch(self):
        pending = _switch_pending(self.device_id)
        if pending is None or pending.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(pending), SWITCH_WAIT_S)
        except asyncio.TimeoutError:
            logger.bind(tag=TAG).warning(f"claude_agent: chat switch still in flight after {SWITCH_WAIT_S} s")

    def _request(self):
        body = {
            "device_id": self.device_id,
            "text": self.text,
            "lang": self.lang,
            "origin": {"kind": "voice", "gateway_session_id": self.conn.session_id, "turn_seq": self.seq},
        }
        headers = {
            "Accept": "text/event-stream",
            "Idempotency-Key": f"{self.device_id}:{self.conn.session_id}:{self.seq}",
            "X-Trace-Id": self.metrics.trace,
        }
        return f"{self.provider.base_url}/v1/chats/current/messages", body, headers

    async def _post(self):
        """("stream", response) | ("retry", why) | ("fail", why) | ("speak", line)."""
        import aiohttp

        url, body, headers = self._request()
        self.metrics.post_at = self.metrics.post_at or time.monotonic()
        try:
            resp = await self.session.post(url, json=body, headers=headers)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return "retry", f"connect {type(e).__name__}"
        if resp.status in (200, 201) and "text/event-stream" in resp.headers.get("Content-Type", ""):
            return "stream", resp
        code, speak = await self._error_of(resp)
        resp.release()
        if resp.status == 503 and speak:
            return "speak", speak
        if resp.status >= 500:
            return "retry", f"HTTP {resp.status} {code}"
        return "fail", f"HTTP {resp.status} {code}"

    @staticmethod
    async def _error_of(resp):
        try:
            error = (await resp.json(content_type=None)).get("error") or {}
        except Exception:
            return "-", None
        detail = error.get("detail") if isinstance(error.get("detail"), dict) else {}
        speak = detail.get("speak") if isinstance(detail.get("speak"), str) else None
        return str(error.get("code") or "-")[:40], speak

    async def _exchange(self):
        retries = 1
        while True:
            kind, value = await self._post()
            if kind == "retry" and retries:
                retries -= 1
                logger.bind(tag=TAG).warning(f"claude_agent retry ({value}) trace={self.metrics.trace}")
                await asyncio.sleep(RETRY_DELAY_S)
                continue
            if kind in ("retry", "fail"):
                return self._failed(value)
            if kind == "speak":
                self.metrics.reply_end = "busy"
                self._put("speak", value)
                return None
            resp = value
            self.chat_id = resp.headers.get("X-Chat-Id") or self.chat_id
            self.turn_id = resp.headers.get("X-Turn-Id") or self.turn_id
            try:
                ended = await self._read(resp)
            finally:
                resp.close()
            if ended:
                return self._ended()
            if self.accepted or not retries:
                break
            retries -= 1
            await asyncio.sleep(RETRY_DELAY_S)
        if self.accepted and self.chat_id and await self._resume():
            if self.metrics.reply_end != "gateway_closed":
                return self._ended()
        return self._failed("stream stalled or dropped")

    async def _resume(self):
        """Once, after a drop or stall: the chat's events after the last seq, this turn's only, until reply_end."""
        import aiohttp

        url = f"{self.provider.base_url}/v1/chats/{quote(self.chat_id, safe='')}/events?after={self.last_seq}"
        logger.bind(tag=TAG).warning(f"claude_agent resume after={self.last_seq} trace={self.metrics.trace}")
        try:
            resp = await self.session.get(url, headers={"Accept": "text/event-stream"})
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False
        try:
            if resp.status != 200:
                return False
            return await self._read(resp)
        finally:
            resp.close()

    async def _read(self, resp):
        """True once this reply's reply_end arrived; False at a stall, drop or end of stream."""
        import aiohttp

        event, data = None, []
        try:
            while True:
                raw = await resp.content.readline()
                if not raw:
                    return False
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith(":"):
                    self._put("hb")
                elif line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line:
                    if data and self._event(event, "\n".join(data)):
                        return True
                    event, data = None, []
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, ValueError) as e:
            logger.bind(tag=TAG).warning(f"claude_agent stream {type(e).__name__} trace={self.metrics.trace}")
            return False

    def _event(self, name, payload):
        try:
            event = json.loads(payload)
        except ValueError:
            return False
        if not isinstance(event, dict):
            return False
        kind = event.get("type") or name
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if isinstance(event.get("seq"), int):
            self.last_seq = max(self.last_seq, event["seq"])
        mine = event.get("turn_id") == self.turn_id
        if kind == "accepted" and (self.reply_id is None or data.get("reply_id") == self.reply_id):
            self.accepted = True
            self.reply_id = data.get("reply_id")
            self.chat_id = event.get("chat_id") or self.chat_id
            self.turn_id = event.get("turn_id")
            self.metrics.chat = self.chat_id
            self.metrics.routed = data.get("routed")
        elif kind == "text" and self.accepted and mine:
            delta = data.get("delta")
            if isinstance(delta, str) and delta:
                self.spoken = True
                self.metrics.text(delta)
                self._put("text", delta)
        elif kind == "progress" and mine and data.get("phase") == "tool":
            self.metrics.tool()
        elif kind == "device_action" and self.accepted and mine:
            action_id = data.get("action_id")
            if not isinstance(action_id, str) or not _ACTION_ID.match(action_id):
                return False
            if action_id in self.action_ids:
                logger.bind(tag=TAG).info(
                    f"device_action {action_id} replayed: not run again trace={self.metrics.trace}"
                )
                return False
            self.action_ids.add(action_id)
            task = asyncio.get_running_loop().create_task(self._device_action(data))
            self.actions.add(task)
            task.add_done_callback(self.actions.discard)
        elif kind == "reply_end" and self.accepted and data.get("reply_id") == self.reply_id:
            self.metrics.reply_end = data.get("reason")
            self.metrics.reply_end_at = time.monotonic()
            return True
        return False

    async def _device_action(self, data):
        action_id, tool = data["action_id"], data.get("tool")
        result = await run_device_tool(self.conn, tool, data.get("args", {}))
        logger.bind(tag=TAG).info(
            f"device_action {tool} ok={result['ok']} action={action_id} trace={self.metrics.trace}"
        )
        url = (f"{self.provider.base_url}/v1/chats/{quote(self.chat_id, safe='')}"
               f"/device_actions/{quote(action_id, safe='')}")
        try:
            async with self.session.post(url, json=result) as resp:
                if resp.status >= 400:
                    logger.bind(tag=TAG).warning(f"device_action result for {action_id}: HTTP {resp.status}")
        except Exception as e:
            logger.bind(tag=TAG).warning(f"device_action result for {action_id}: {type(e).__name__}")

    def _ended(self):
        self.provider.breaker.success()
        return None

    def _failed(self, why):
        self.provider.breaker.failure()
        self.metrics.reply_end = "fallback"
        logger.bind(tag=TAG).warning(
            f"claude_agent turn failed ({why}) trace={self.metrics.trace} spoken={self.spoken}"
        )
        self._put("speak", (LOST if self.spoken else FALLBACK)[_spoken_lang(self.lang)])
        return None


class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        import os

        self.base_url = (config.get("url") or os.environ.get("AGENT_SERVICE_URL") or "").rstrip("/")
        self.token = os.environ.get("AGENT_SERVICE_TOKEN", "")
        if not self.base_url or not self.token:
            logger.bind(tag=TAG).error("claude_agent: AGENT_SERVICE_URL or AGENT_SERVICE_TOKEN is not set")
        self.breaker = Breaker()

    def response_no_stream(self, system_prompt, user_prompt, **kwargs):
        logger.bind(tag=TAG).warning("claude_agent serves voice turns only; a non-streaming request got no reply")
        return ""

    def response(self, session_id, dialogue, **kwargs):
        import weixue_patches

        conn = weixue_patches.session_connection(session_id)
        text = _last_user_text(dialogue)
        if conn is None or getattr(conn, "loop", None) is None:
            logger.bind(tag=TAG).error("claude_agent: no connection for this session")
            yield FALLBACK["en"]
            return
        if not text:
            return
        weixue_patches.stop_agent_stream(conn)
        sid = conn.sentence_id
        lang = turn_language(conn, text)
        seq = getattr(conn, "weixue_agent_turns", 0) + 1
        conn.weixue_agent_turns = seq
        metrics = self._metrics(conn, sid)
        conn.weixue_turn_metrics = metrics
        record = weixue_patches.track_playback(conn, sid)
        turn = None
        try:
            if not self.breaker.allow():
                logger.bind(tag=TAG).warning(f"claude_agent breaker open: fallback without a request trace={metrics.trace}")
                metrics.reply_end = "breaker"
                _speak(conn, sid, FALLBACK[_spoken_lang(lang)])
                return
            turn = AgentTurn(self, conn, text, lang, seq, metrics)
            conn.weixue_agent_stream = turn
            try:
                turn.start()
            except RuntimeError:
                logger.bind(tag=TAG).warning(f"claude_agent: the connection's event loop is closed trace={metrics.trace}")
                return
            while True:
                try:
                    item = turn.items.get(timeout=QUEUE_WAIT_S)
                except queue.Empty:
                    logger.bind(tag=TAG).error(f"claude_agent: no stream item in {QUEUE_WAIT_S} s trace={metrics.trace}")
                    metrics.reply_end = "fallback"
                    _speak(conn, sid, FALLBACK[_spoken_lang(lang)])
                    break
                if item is _END or turn.cancelled.is_set() or conn.client_abort or conn.sentence_id != sid:
                    break
                kind, value = item
                if kind == "text":
                    yield value
                elif kind == "hb":
                    yield ""
                elif kind == "speak":
                    _speak(conn, sid, value)
        finally:
            if turn is not None:
                turn.cancel()
                if getattr(conn, "weixue_agent_stream", None) is turn:
                    conn.weixue_agent_stream = None
            try:
                asyncio.run_coroutine_threadsafe(report_when_played(conn, metrics, record), conn.loop)
            except RuntimeError:
                metrics.report()

    @staticmethod
    def _metrics(conn, sid):
        now = time.monotonic()
        speech_end = getattr(conn, "weixue_speech_end", None)
        if speech_end is not None and now - speech_end > SPEECH_END_MAX_AGE_S:
            speech_end = None
        stt = getattr(conn, "weixue_stt", None) or {}
        stt_ms = stt.get("latency_ms") if speech_end is not None and stt.get("at", 0) >= speech_end else None
        return TurnMetrics(sid, uuid.uuid4().hex, speech_end=speech_end, stt_ms=stt_ms)


def _speak(conn, sid, text):
    """A gateway line on the turn's sentence, bypassing chat()'s history."""
    if conn.sentence_id == sid and not conn.client_abort:
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(sentence_id=sid, sentence_type=SentenceType.MIDDLE,
                          content_type=ContentType.TEXT, content_detail=text)
        )
