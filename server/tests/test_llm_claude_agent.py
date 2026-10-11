"""claude_agent provider against a fake agent-service (aiohttp SSE server on the connection's event loop),
driven through upstream chat() with Intent nointent: reply_end reasons, heartbeats, barge-in, retry,
stall and resume, breaker, switch wait, device_action whitelist, metrics; the wake cue fence."""
import asyncio
import json
import socket
import threading
import time
import uuid

import pytest
from aiohttp import web

import weixue_agent_delivery as wad
import weixue_patches
from core.providers.llm.claude_agent import claude_agent as ca
from core.providers.tts.dto.dto import ContentType, SentenceType
from tests.conftest import LOGGER
from tests.upstream_chat import make_connection, spoken

CHAT = "cht_01HZZZZZZZZZZZZZZZZZZZZZZZ"
TURN = "trn_01HZZZZZZZZZZZZZZZZZZZZZZZ"
OTHER_TURN = "trn_01HYYYYYYYYYYYYYYYYYYYYYYY"
REPLY = "rpl_01HZZZZZZZZZZZZZZZZZZZZZZZ"
ACTION = "dac_01HZZZZZZZZZZZZZZZZZZZZZZZ"
DEVICE = "aa:bb:cc:dd:ee:01"
TOKEN = "t" * 64


class FakeService:
    """Scripted agent-service: post_script (one callable, or one per attempt) answers
    POST /v1/chats/current/messages; resume_script answers GET /v1/chats/{id}/events."""

    def __init__(self):
        self.posts = []
        self.gets = []
        self.results = {}
        self.result_posts = []
        self.closed_at = []
        self.seq = 0
        self.post_script = None
        self.resume_script = None
        self.base = None
        self._runner = None

    async def start(self):
        app = web.Application()
        app.router.add_post("/v1/chats/current/messages", self._messages)
        app.router.add_get("/v1/chats/{chat}/events", self._events)
        app.router.add_post("/v1/chats/{chat}/device_actions/{action}", self._device_action)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"

    async def stop(self):
        await self._runner.cleanup()

    async def _messages(self, request):
        self.posts.append({"at": time.monotonic(), "headers": dict(request.headers), "body": await request.json()})
        script = self.post_script
        if isinstance(script, list):
            script = script[min(len(self.posts), len(script)) - 1]
        return await script(self, request)

    async def _events(self, request):
        self.gets.append({"after": request.query.get("after"), "chat": request.match_info["chat"]})
        return await self.resume_script(self, request)

    async def _device_action(self, request):
        self.results[request.match_info["action"]] = await request.json()
        self.result_posts.append(request.match_info["action"])
        return web.json_response({"action_id": request.match_info["action"], "accepted": True})

    # Script helpers

    async def open(self, request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "X-Chat-Id": CHAT, "X-Turn-Id": TURN})
        await resp.prepare(request)
        return resp

    async def send(self, resp, type_, data, turn=TURN):
        self.seq += 1
        event = {"v": 1, "seq": self.seq, "chat_id": CHAT, "task_id": None, "turn_id": turn, "trace_id": "0" * 32,
                 "ts": "2026-10-11T00:00:00Z", "type": type_, "data": data}
        await resp.write(f"id: {self.seq}\nevent: {type_}\ndata: {json.dumps(event)}\n\n".encode())

    async def accepted(self, resp, routed="new_task"):
        await self.send(resp, "accepted", {"routed": routed, "task_state": "progress", "reply_id": REPLY})

    async def reply_end(self, resp, reason="turn_done"):
        await self.send(resp, "reply_end", {"reason": reason, "ack_text": None, "reply_id": REPLY})

    async def hb(self, resp):
        await resp.write(b": hb\n\n")

    async def hold(self, request, seconds):
        """Waits until the client closes the connection (its time is kept) or seconds pass."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            transport = request.transport
            if transport is None or transport.is_closing():
                self.closed_at.append(time.monotonic())
                return True
            await asyncio.sleep(0.005)
        return False


def reply(*texts, reason="turn_done", heartbeats=0):
    async def script(svc, request):
        resp = await svc.open(request)
        await svc.accepted(resp)
        for text in texts:
            await svc.send(resp, "text", {"delta": text})
        for _ in range(heartbeats):
            await svc.hb(resp)
            await asyncio.sleep(0.05)
        await svc.reply_end(resp, reason)
        return resp
    return script


def status(code, error="internal", detail=None):
    async def script(svc, request):
        return web.json_response({"error": {"code": error, "message": "x", "detail": detail}}, status=code)
    return script


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop

    async def drain():
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run_coroutine_threadsafe(drain(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


@pytest.fixture
def svc(loop):
    service = FakeService()
    asyncio.run_coroutine_threadsafe(service.start(), loop).result(5)
    yield service
    asyncio.run_coroutine_threadsafe(service.stop(), loop).result(5)


@pytest.fixture
def provider(svc, monkeypatch):
    monkeypatch.setenv("AGENT_SERVICE_TOKEN", TOKEN)
    return ca.LLMProvider({"type": "claude_agent", "url": svc.base})


@pytest.fixture
def conn(provider, loop):
    c = make_connection(provider, [])
    c.intent_type = "nointent"
    c.loop = loop
    c.session_id = uuid.uuid4().hex
    c.device_id = DEVICE
    c.headers = {"device-id": DEVICE}
    c.stop_event = threading.Event()
    weixue_patches._SESSIONS[c.session_id] = c
    yield c
    weixue_patches._SESSIONS.pop(c.session_id, None)


def drain(conn):
    messages = []
    while not conn.tts.tts_text_queue.empty():
        messages.append(conn.tts.tts_text_queue.get_nowait())
    return messages


def history(conn):
    return [m.content for m in conn.dialogue.dialogue if m.role == "assistant"]


def chat(conn, query="What time is it?"):
    conn.chat(query)
    return drain(conn)


def in_background(conn, query="Tell me a story"):
    thread = threading.Thread(target=conn.chat, args=(query,), daemon=True)
    thread.start()
    return thread


def wait(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.005)
    raise AssertionError("timed out")


def latency_line(conn):
    conn.sentence_id = "next-turn"
    return wait(lambda: next((m for _, m in LOGGER.records if m.startswith("turn_latency")), None))


def dead_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ turn mapping


@pytest.mark.parametrize("reason", ["turn_done", "hold_elapsed", "ack", "question", "gateway_closed"])
def test_reply_text_is_spoken_and_the_turn_ends_at_reply_end(svc, conn, reason):
    svc.post_script = reply("It is ", "ten past nine.", reason=reason)
    messages = chat(conn)
    assert messages[0].sentence_type == SentenceType.FIRST and messages[-1].sentence_type == SentenceType.LAST
    assert spoken(messages) == "It is ten past nine."
    assert history(conn) == ["It is ten past nine."]
    line = latency_line(conn)
    assert f"reply_end={reason}" in line and f"chat={CHAT}" in line and "routed=new_task" in line


def test_request_carries_the_last_user_message_and_turn_identity(svc, conn):
    svc.post_script = reply("Okay.")
    chat(conn, "Turn on the kitchen light")
    chat(conn, "And the hall")
    first, second = svc.posts
    assert first["body"] == {"device_id": DEVICE, "text": "Turn on the kitchen light", "lang": "en",
                             "origin": {"kind": "voice", "gateway_session_id": conn.session_id, "turn_seq": 1}}
    assert second["body"]["text"] == "And the hall" and second["body"]["origin"]["turn_seq"] == 2
    assert first["headers"]["Idempotency-Key"] == f"{DEVICE}:{conn.session_id}:1"
    assert first["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert first["headers"]["Accept"] == "text/event-stream"
    assert len(first["headers"]["X-Trace-Id"]) == 32
    chat(conn, "打开客厅的灯")
    assert svc.posts[2]["body"]["lang"] == "zh"


def test_heartbeats_yield_empty_text(svc, conn):
    svc.post_script = reply("Hello.", heartbeats=3)
    conn.sentence_id = "s1"
    items = list(conn.llm.response(conn.session_id, [{"role": "user", "content": "hi"}]))
    assert items.count("") >= 3 and "".join(items) == "Hello."


def test_text_of_another_turn_and_other_events_are_not_spoken(svc, conn):
    async def script(svc, request):
        resp = await svc.open(request)
        await svc.accepted(resp)
        await svc.send(resp, "text", {"delta": "Not mine."}, turn=OTHER_TURN)
        await svc.send(resp, "progress", {"phase": "tool", "tool": "ha_get_state"})
        await svc.send(resp, "text", {"delta": "Mine."})
        await svc.reply_end(resp)
        return resp
    svc.post_script = script
    assert spoken(chat(conn)) == "Mine."


# ------------------------------------------------------------------ barge-in and stale turns


def _hold_after_text(text="First part. "):
    async def script(svc, request):
        resp = await svc.open(request)
        await svc.accepted(resp)
        await svc.send(resp, "text", {"delta": text})
        if not await svc.hold(request, 5):
            return resp
        try:
            await svc.send(resp, "text", {"delta": "Stale words."})
        except Exception:
            pass
        return resp
    return script


def test_abort_closes_the_stream_at_once_and_queues_no_stale_text(svc, conn, loop):
    svc.post_script = _hold_after_text()
    thread = in_background(conn)
    wait(lambda: conn.tts.tts_text_queue.qsize() >= 2)

    async def upstream_abort(c):
        c.client_abort = True

    started = time.monotonic()
    asyncio.run_coroutine_threadsafe(weixue_patches.count_aborts(upstream_abort)(conn), loop).result(2)
    wait(lambda: svc.closed_at)
    assert svc.closed_at[0] - started < 0.1
    thread.join(2)
    assert not thread.is_alive()
    said = spoken(drain(conn))
    assert said == "First part. " and "Stale" not in "".join(history(conn))
    assert conn.weixue_agent_stream is None


def test_a_newer_turn_closes_the_previous_stream(svc, conn):
    svc.post_script = [_hold_after_text("Old reply. "), reply("New reply.")]
    first = in_background(conn, "Tell me a story")
    wait(lambda: conn.tts.tts_text_queue.qsize() >= 2)
    second = in_background(conn, "Actually, what time is it?")
    first.join(3)
    second.join(3)
    wait(lambda: svc.closed_at)
    assert len(svc.posts) == 2
    assert "Stale" not in spoken(drain(conn))


# ------------------------------------------------------------------ errors


def test_5xx_before_accepted_is_retried_once_with_the_same_key(svc, conn):
    svc.post_script = [status(502), reply("Here you go.")]
    assert spoken(chat(conn)) == "Here you go."
    assert len(svc.posts) == 2
    assert svc.posts[0]["headers"]["Idempotency-Key"] == svc.posts[1]["headers"]["Idempotency-Key"]


def test_unreachable_service_speaks_the_fallback_outside_history(conn, provider):
    provider.base_url = f"http://127.0.0.1:{dead_port()}"
    started = time.monotonic()
    messages = chat(conn)
    assert spoken(messages) == ca.FALLBACK["en"]
    assert messages[-1].sentence_type == SentenceType.LAST
    assert history(conn) == [] and time.monotonic() - started < 2
    assert "reply_end=fallback" in latency_line(conn)


def test_chinese_turn_gets_the_chinese_fallback(conn, provider):
    provider.base_url = f"http://127.0.0.1:{dead_port()}"
    assert spoken(chat(conn, "现在几点")) == ca.FALLBACK["zh"]


def test_busy_speaks_the_service_line_without_retry(svc, conn, provider):
    svc.post_script = status(503, "unavailable", {"reason": "busy", "speak": "I'm busy with other requests."})
    assert spoken(chat(conn)) == "I'm busy with other requests."
    assert len(svc.posts) == 1 and not provider.breaker.failures


@pytest.mark.parametrize("code, error", [(401, "unauthorized"), (400, "invalid_request"), (409, "idempotency_mismatch")])
def test_client_errors_speak_the_fallback_without_retry(svc, conn, code, error, log_records):
    svc.post_script = status(code, error)
    assert spoken(chat(conn)) == ca.FALLBACK["en"]
    assert len(svc.posts) == 1
    assert any(f"HTTP {code} {error}" in m for _, m in log_records)
    assert all(TOKEN not in m for _, m in log_records)


def _stall_after_text(text):
    async def script(svc, request):
        resp = await svc.open(request)
        await svc.accepted(resp)
        if text:
            await svc.send(resp, "text", {"delta": text})
        await svc.hold(request, 3)
        return resp
    return script


def test_stall_after_accepted_resumes_once_from_the_last_seq(svc, conn, monkeypatch):
    monkeypatch.setattr(ca, "STALL_S", 0.4)

    async def resume(svc, request):
        resp = await svc.open(request)
        await svc.send(resp, "text", {"delta": "Somebody else."}, turn=OTHER_TURN)
        await svc.send(resp, "text", {"delta": "Part two."})
        await svc.reply_end(resp)
        await svc.hold(request, 3)
        return resp
    svc.post_script = _stall_after_text("Part one. ")
    svc.resume_script = resume
    assert spoken(chat(conn)) == "Part one. Part two."
    assert svc.gets == [{"after": "2", "chat": CHAT}]


def test_failed_resume_after_speech_says_the_connection_was_lost(svc, conn, monkeypatch):
    monkeypatch.setattr(ca, "STALL_S", 0.3)
    svc.post_script = _stall_after_text("Part one. ")
    svc.resume_script = _stall_after_text(None)
    said = spoken(chat(conn))
    assert said == "Part one. " + ca.LOST["en"]
    assert history(conn) == ["Part one. "] and len(svc.gets) == 1


def test_resumed_reply_cut_by_the_drop_is_lost(svc, conn, monkeypatch):
    monkeypatch.setattr(ca, "STALL_S", 0.3)

    async def resume(svc, request):
        resp = await svc.open(request)
        await svc.reply_end(resp, "gateway_closed")
        return resp
    svc.post_script = _stall_after_text("Part one. ")
    svc.resume_script = resume
    assert spoken(chat(conn)) == "Part one. " + ca.LOST["en"]


def test_stall_before_any_text_speaks_the_fallback(svc, conn, monkeypatch):
    monkeypatch.setattr(ca, "STALL_S", 0.3)
    svc.post_script = _stall_after_text(None)
    svc.resume_script = _stall_after_text(None)
    assert spoken(chat(conn)) == ca.FALLBACK["en"]
    assert history(conn) == []


def test_breaker_opens_after_three_failures_and_closes_on_success(svc, conn, provider, log_records):
    live = provider.base_url
    provider.base_url = f"http://127.0.0.1:{dead_port()}"
    for _ in range(3):
        chat(conn)
    assert not provider.breaker.allow()
    assert any("breaker open" in m for _, m in log_records)
    svc.post_script = reply("Should not be asked.")
    provider.base_url = live
    started = time.monotonic()
    assert spoken(chat(conn)) == ca.FALLBACK["en"]
    assert svc.posts == [] and time.monotonic() - started < 0.5
    provider.breaker.open_until = time.monotonic() - 1
    svc.post_script = reply("Back again.")
    assert spoken(chat(conn)) == "Back again."
    assert provider.breaker.allow() and any("breaker closed" in m for _, m in log_records)


def test_breaker_counts_failures_in_a_sliding_window():
    now = [0.0]
    breaker = ca.Breaker(clock=lambda: now[0])
    breaker.failure()
    now[0] = 61
    breaker.failure()
    breaker.failure()
    assert breaker.allow()
    breaker.failure()
    assert not breaker.allow()
    now[0] = 61 + ca.BREAKER_OPEN_S + 0.1
    assert breaker.allow()


# ------------------------------------------------------------------ chat switch race


def _pending(loop, monkeypatch, resolve_after=None):
    async def make():
        future = asyncio.get_running_loop().create_future()
        if resolve_after is not None:
            asyncio.get_running_loop().call_later(resolve_after, future.set_result, None)
        return future
    future = asyncio.run_coroutine_threadsafe(make(), loop).result(2)
    sink = type("Sink", (), {"pending_switch": staticmethod(lambda device: future if device == DEVICE else None)})
    monkeypatch.setattr(wad, "INSTANCE", type("Gateway", (), {"sink": sink})())
    return future


def test_post_waits_for_an_in_flight_switch(svc, conn, loop, monkeypatch):
    _pending(loop, monkeypatch, resolve_after=0.3)
    svc.post_script = reply("In the new chat.")
    started = time.monotonic()
    assert spoken(chat(conn)) == "In the new chat."
    assert svc.posts[0]["at"] - started >= 0.28


def test_switch_wait_is_bounded(svc, conn, loop, monkeypatch):
    monkeypatch.setattr(ca, "SWITCH_WAIT_S", 0.2)
    _pending(loop, monkeypatch)
    svc.post_script = reply("Anyway.")
    started = time.monotonic()
    assert spoken(chat(conn)) == "Anyway."
    assert 0.18 <= svc.posts[0]["at"] - started < 1.0


# ------------------------------------------------------------------ device_action


class FakeMcp:
    def __init__(self):
        self.name_mapping = {
            "self_screen_set_bedside": "self.screen.set_bedside",
            "self_audio_speaker_set_volume": "self.audio_speaker.set_volume",
            "self_diag_gesture": "self.diag.gesture",
        }

    async def is_ready(self):
        return True


def _device_script(tool, args):
    async def script(svc, request):
        resp = await svc.open(request)
        await svc.accepted(resp)
        await svc.send(resp, "text", {"delta": "Okay. "})
        await svc.send(resp, "device_action", {"action_id": ACTION, "tool": tool, "args": args})
        deadline = time.monotonic() + 3
        while ACTION not in svc.results and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        await svc.send(resp, "text", {"delta": "Done."})
        await svc.reply_end(resp)
        return resp
    return script


@pytest.fixture
def device_calls(monkeypatch, conn):
    import sys
    import types

    calls = []

    async def call_mcp_tool(c, client, name, args, timeout=30):
        calls.append((name, json.loads(args), timeout))
        return '{"night":true}'

    monkeypatch.setitem(sys.modules, "core.providers.tools.device_mcp",
                        types.SimpleNamespace(call_mcp_tool=call_mcp_tool))
    conn.mcp_client = FakeMcp()
    return calls


def test_device_action_runs_on_the_device_and_acks_the_result(svc, conn, device_calls):
    svc.post_script = _device_script("self.screen.set_bedside", {"mode": "night"})
    assert spoken(chat(conn, "Set bedside mode")) == "Okay. Done."
    assert device_calls == [("self_screen_set_bedside", {"mode": "night"}, ca.DEVICE_CALL_TIMEOUT_S)]
    assert svc.results[ACTION] == {"ok": True, "result": '{"night":true}'}


def test_a_replayed_device_action_runs_once(svc, conn, device_calls, monkeypatch):
    monkeypatch.setattr(ca, "STALL_S", 0.4)
    action = {"action_id": ACTION, "tool": "self.screen.set_bedside", "args": {"mode": "night"}}

    async def post(svc, request):
        resp = await svc.open(request)
        await svc.accepted(resp)
        await svc.send(resp, "text", {"delta": "Okay. "})
        await svc.send(resp, "device_action", action)
        await svc.send(resp, "device_action", action)
        await svc.hold(request, 3)
        return resp

    async def resume(svc, request):
        resp = await svc.open(request)
        await svc.send(resp, "device_action", action)
        await svc.send(resp, "text", {"delta": "Done."})
        await svc.reply_end(resp)
        return resp
    svc.post_script = post
    svc.resume_script = resume
    assert spoken(chat(conn, "Set bedside mode")) == "Okay. Done."
    assert len(svc.gets) == 1
    assert device_calls == [("self_screen_set_bedside", {"mode": "night"}, ca.DEVICE_CALL_TIMEOUT_S)]
    assert svc.result_posts == [ACTION]


@pytest.mark.parametrize("tool", ["self.diag.gesture", "self.system.reconfigure_wifi", "self.camera.take_photo"])
def test_device_action_outside_the_agent_visible_list_is_refused(svc, conn, device_calls, tool):
    svc.post_script = _device_script(tool, {})
    chat(conn, "Pick yourself up")
    assert device_calls == []
    assert svc.results[ACTION] == {"ok": False, "error": "this device tool is not available"}


def test_device_action_reports_a_missing_tool_or_channel(conn, loop, device_calls):
    run = lambda tool, args: asyncio.run_coroutine_threadsafe(ca.run_device_tool(conn, tool, args), loop).result(2)
    assert run("self.imu.get_state", {}) == {"ok": False, "error": "the device does not offer this tool"}
    assert run("self.audio_speaker.set_volume", "loud") == {"ok": False, "error": "args must be an object"}
    conn.mcp_client = None
    assert run("self.audio_speaker.set_volume", {"volume": 3}) == {
        "ok": False, "error": "the device MCP channel is not ready"}
    assert device_calls == []


def test_device_action_whitelist_matches_the_service():
    import ast
    from tests.conftest import SERVER_DIR

    tree = ast.parse((SERVER_DIR / "agent-service" / "weixue_agent" / "app_tools.py").read_text())
    service = next(ast.literal_eval(n.value) for n in tree.body
                   if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "DEVICE_TOOLS")
    import weixue_device_tools

    assert set(service) == ca.DEVICE_ACTION_TOOLS == weixue_device_tools.AGENT_VISIBLE_DEVICE_TOOLS
    assert not any(t.startswith(("self.diag", "self.camera")) or "wifi" in t for t in ca.DEVICE_ACTION_TOOLS)


# ------------------------------------------------------------------ metrics


def test_latency_line_fields():
    m = ca.TurnMetrics("sid", "a" * 32, speech_end=10.0, stt_ms=250, barge_in=False)
    m.chat, m.routed, m.reply_end = CHAT, "new_task", "turn_done"
    m.post_at = 10.3
    m.clock = lambda: 10.9
    m.text("Let me check. ")
    m.tool()
    m.text("The light is on.")
    m.reply_end_at = 12.0
    m.segments = [{"text": "Let me check", "handed_at": 10.95, "first": 11.2},
                  {"text": "The light is on", "handed_at": 12.0, "first": 12.3}]
    assert m.line() == (
        f"turn_latency trace={'a' * 32} chat={CHAT} routed=new_task reply_end=turn_done stt_final_latency_ms=250 "
        "service_first_text_ms=600 tts_first_audio_ms=250 total_ms=1200 answer_audio_ms=2300 reply_end_ms=1700 "
        "barge_in=0"
    )


def test_latency_line_without_a_tool_takes_the_first_audio_as_the_answer():
    m = ca.TurnMetrics("sid", "b" * 32, speech_end=1.0)
    m.segments = [{"text": "Ten past nine", "handed_at": 1.5, "first": 1.7}]
    line = m.line()
    assert "total_ms=700 answer_audio_ms=700" in line and "stt_final_latency_ms=-" in line
    assert "service_first_text_ms=-" in line


def test_turn_latency_uses_the_utterance_timings_and_playback_segments(svc, conn):
    conn.weixue_speech_end = time.monotonic()
    conn.weixue_stt = {"at": time.monotonic(), "latency_ms": 240, "language": "en-US"}
    svc.post_script = reply("Ten past nine.")
    chat(conn)
    metrics = conn.weixue_turn_metrics
    metrics.segments.append({"text": "Ten past nine", "handed_at": time.monotonic(), "first": time.monotonic()})
    line = latency_line(conn)
    assert "stt_final_latency_ms=240" in line and "reply_end=turn_done" in line
    assert all(f"{key}=-" not in line for key in ("service_first_text_ms", "total_ms", "tts_first_audio_ms",
                                                  "answer_audio_ms", "reply_end_ms"))


async def test_playback_hooks_feed_the_turn_metrics():
    class Conn:
        sentence_id = "s1"
        client_abort = False

    conn = Conn()
    conn.weixue_turn_metrics = ca.TurnMetrics("s1", "c" * 32)

    async def send(c, kind, audios, text, sentence_id=None):
        return None

    async def write(c, packet, flow_control):
        return None

    wrapped = weixue_patches.watch_sent_audio(send, "FIRST", "LAST")
    transport = weixue_patches.watch_transport(write)
    await wrapped(conn, "FIRST", [b"a", b"b"], "Ten past nine", "s1")
    await transport(conn, b"a", {"sentence_id": "s1"})
    segment = conn.weixue_turn_metrics.segments[0]
    assert segment["text"] == "Ten past nine" and segment["first"] >= segment["handed_at"]
    await wrapped(conn, "FIRST", [b"c"], "Other turn", "s2")
    assert len(conn.weixue_turn_metrics.segments) == 1


async def test_speech_end_is_noted_at_the_vad_stop():
    seen = []

    class Provider:
        async def handle_voice_stop(self, conn, audio):
            seen.append(conn.weixue_speech_end)

    conn = type("Conn", (), {})()
    before = time.monotonic()
    await weixue_patches.note_speech_end(Provider.handle_voice_stop)(Provider(), conn, [b"x"])
    assert seen and seen[0] >= before


# ------------------------------------------------------------------ provider contract


def test_session_registry_follows_the_connection():
    class Proactive:
        auth = type("Auth", (), {"device": staticmethod(lambda headers: None)})()

    closed = []

    class Stream:
        def cancel(self):
            closed.append(True)

    class Conn:
        session_id = "sess-1"

    async def handle(self, ws):
        assert weixue_patches.session_connection("sess-1") is self
        self.weixue_agent_stream = Stream()

    conn = Conn()
    ws = type("Ws", (), {"request": type("Req", (), {"headers": {}})()})()
    asyncio.run(weixue_patches.register_sessions(handle, Proactive())(conn, ws))
    assert weixue_patches.session_connection("sess-1") is None and closed == [True]


def test_non_streaming_requests_get_no_reply(provider, svc):
    assert provider.response_no_stream("system", "summarize this") == ""
    assert svc.posts == []


def test_unknown_session_speaks_the_fallback(provider):
    assert list(provider.response("no-such-session", [{"role": "user", "content": "hi"}])) == [ca.FALLBACK["en"]]


# ------------------------------------------------------------------ wake cue fence


async def test_no_cue_frame_follows_a_turns_first():
    from tests.test_wake_cue import CUES, REMOVE_PUNCTUATION, Conn, Recorder, Upstream, _player

    rec = Recorder()
    conn = Conn()
    conn.client_abort = False
    wake = weixue_patches.acknowledge_wake(Upstream.handle, REMOVE_PUNCTUATION, _player(rec))
    await wake(Upstream(), conn, {"state": "detect", "text": "Jarvis"})
    turn = []

    async def start_to_chat(c, text):
        c.sentence_id = "user-turn"
        c.client_is_speaking = True
        turn.append(time.monotonic())
        await rec.send_audio_message(c, "FIRST", [], None, "user-turn")

    await weixue_patches.guard_playback_echo(start_to_chat, LOGGER)(conn, "what time is it")
    await asyncio.sleep(weixue_patches.CUE_DELAY_S + weixue_patches.DECODER_RESET_S + 0.2)
    assert turn and conn.weixue_wake_cue.cancelled()
    cue_frames = [e for e in rec.events if e[0] == "audio" and e[3] in [c["text"] for c in CUES]]
    assert cue_frames == [] and not any(e[0] == "tts" for e in rec.events)
