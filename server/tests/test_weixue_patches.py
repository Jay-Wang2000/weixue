"""weixue_patches direct_answer decoding, driven through upstream ConnectionHandler.chat()
(loaded from the vendored core/connection.py) with the Anthropic provider on a fake stream."""
import json
import sys
import types
from types import SimpleNamespace

import pytest

import weixue_patches
from core.providers.llm.anthropic.anthropic import DIRECT_ANSWER, LLMProvider
from tests.test_conversation_timing import stub_handler_modules
from tests.upstream_chat import UpstreamChat, make_connection, run_chat, spoken

UPSTREAM_CLEAN = UpstreamChat.__dict__["_clean_response_garbage"]
UPSTREAM_EXTRACT = UpstreamChat.__dict__["_extract_direct_answer_response"]

REPLIES = [
    "Two plus two is four.",
    "Say 'on' now please.",
    'He said "hi" and left.',
    "Path C:\\temp, then (done).",
    "二加二等于四。OK, 好的",
    "客厅灯已打开。",
    "Line one.\nLine two",
    "  spaced  out  ",
    "Smile \U0001F600 now",
]


class _Stream:
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self.events)

    def get_final_message(self):
        return SimpleNamespace(stop_reason="tool_use", usage=None, stop_details=None)


def _llm(arguments, cuts):
    events = [SimpleNamespace(
        type="content_block_start", index=0,
        content_block=SimpleNamespace(type="tool_use", id="toolu_da", name=DIRECT_ANSWER, input={}),
    )]
    bounds = [0, *cuts, len(arguments)]
    for a, b in zip(bounds, bounds[1:]):
        events.append(SimpleNamespace(
            type="content_block_delta", index=0,
            delta=SimpleNamespace(type="input_json_delta", partial_json=arguments[a:b]),
        ))
    events.append(SimpleNamespace(type="content_block_stop", index=0))
    provider = LLMProvider({"api_key": "sk-test"})
    provider.client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **_p: _Stream(events)))
    return provider


def _patched(cls):
    cls._extract_direct_answer_response = staticmethod(weixue_patches.decode_direct_answer)
    cls._clean_response_garbage = staticmethod(weixue_patches.keep_decoded_text)


def _speak(reply, cuts, patch=_patched, ensure_ascii=False):
    arguments = json.dumps({"response": reply}, ensure_ascii=ensure_ascii)
    conn = make_connection(_llm(arguments, cuts), [], patch=patch)
    messages = run_chat(conn)
    return spoken(messages), conn.dialogue.dialogue[-1].content


def _two_way_cuts(reply, ensure_ascii=False):
    n = len(json.dumps({"response": reply}, ensure_ascii=ensure_ascii))
    return [[c] for c in range(1, n)]


def test_upstream_cleaner_corrupts_text_at_chunk_edges():
    corrupted = {
        _speak(reply, cuts, patch=None)[0]
        for reply in ("Two plus two is four.", "Say 'on' now please.")
        for cuts in _two_way_cuts(reply)
    }
    assert {"Two plus two isfour.", "Say 'onnow please."} <= corrupted


@pytest.mark.parametrize("reply", REPLIES)
@pytest.mark.parametrize("ensure_ascii", [False, True])
def test_every_split_speaks_and_stores_the_exact_reply(reply, ensure_ascii):
    for cuts in _two_way_cuts(reply, ensure_ascii):
        assert _speak(reply, cuts, ensure_ascii=ensure_ascii) == (reply, reply), cuts


@pytest.mark.parametrize("reply", REPLIES)
def test_one_character_deltas_speak_the_exact_reply(reply):
    n = len(json.dumps({"response": reply}, ensure_ascii=True))
    assert _speak(reply, list(range(1, n)), ensure_ascii=True) == (reply, reply)


@pytest.mark.parametrize("reply", REPLIES)
@pytest.mark.parametrize("ensure_ascii", [False, True])
def test_decoded_prefixes_only_grow(reply, ensure_ascii):
    arguments = json.dumps({"response": reply}, ensure_ascii=ensure_ascii)
    previous = ""
    for end in range(len(arguments) + 1):
        decoded = weixue_patches.decode_direct_answer(arguments[:end])
        assert reply.startswith(decoded) and decoded.startswith(previous), arguments[:end]
        previous = decoded
    assert previous == reply


@pytest.mark.parametrize(
    "arguments, expected",
    [
        ("", ""),
        ("{", ""),
        ('{"response"', ""),
        ('{"response": ', ""),
        ('{"response": "ab\\', "ab"),
        ('{"response": "ab\\u00', "ab"),
        ('{"response": "ab\\ud83d', "ab"),
        ('{"response": "ab\\ud83d\\u', "ab"),
        ('{"response": "ab\\ud83dxyz1234', "ab\ufffdxyz1234"),
        ('{"response": "ok"', "ok"),
        ('{"response": "ok"}', "ok"),
        ('{"response": 3}', ""),
        ('{"other": "x"}', ""),
    ],
)
def test_decode_partial_arguments(arguments, expected):
    assert weixue_patches.decode_direct_answer(arguments) == expected


def test_upstream_cleaner_strips_content_quotes():
    assert UPSTREAM_EXTRACT('{"response": "Say \'on\'') == "Say 'on'"
    assert UPSTREAM_CLEAN("Say 'on'") == "Say 'on"
    assert weixue_patches.decode_direct_answer('{"response": "four."}  ') == "four."


def test_apply_patches_each_hook_once(monkeypatch):
    connection = types.ModuleType("core.connection")
    listen = types.ModuleType("core.handle.textHandler.listenMessageHandler")
    http_server = types.ModuleType("core.http_server")

    async def handle_connection(self, ws):
        return None

    async def handle(self, conn, msg_json):
        return None

    class ConnectionHandler:
        _clean_response_garbage = staticmethod(UPSTREAM_CLEAN)
        _extract_direct_answer_response = staticmethod(UPSTREAM_EXTRACT)

    ConnectionHandler.handle_connection = handle_connection

    class ListenTextMessageHandler:
        pass

    ListenTextMessageHandler.handle = handle
    web = types.SimpleNamespace(Application=lambda: types.SimpleNamespace(routes=[]))

    connection.ConnectionHandler = ConnectionHandler
    listen.ListenTextMessageHandler = ListenTextMessageHandler
    http_server.web = web
    monkeypatch.setitem(sys.modules, "core.connection", connection)
    monkeypatch.setitem(sys.modules, "core.handle.textHandler.listenMessageHandler", listen)
    monkeypatch.setitem(sys.modules, "core.http_server", http_server)
    stub_handler_modules(monkeypatch)
    from tests.test_conversation_timing import stub_playback_modules

    stub_playback_modules(monkeypatch, http_server)

    weixue_patches.apply()
    weixue_patches.apply()

    assert ConnectionHandler.__dict__["_clean_response_garbage"].__func__ is weixue_patches.keep_decoded_text
    assert (
        ConnectionHandler.__dict__["_extract_direct_answer_response"].__func__
        is weixue_patches.decode_direct_answer
    )
    assert ConnectionHandler()._clean_response_garbage("two is ") == "two is "
    assert ConnectionHandler()._extract_direct_answer_response('{"response": "a\\"b') == 'a"b'
    assert ConnectionHandler.handle_connection.weixue_wrapped is handle_connection
    assert ListenTextMessageHandler.handle.weixue_wrapped.weixue_wrapped is handle
    assert isinstance(http_server.web, weixue_patches.WebWithRoutes)
    assert http_server.web._web is web
    ota = sys.modules["core.api.ota_handler"].OTAHandler
    assert ota.handle_post.weixue_allowlist.__name__ == "handle_post"
    assert not hasattr(ota.handle_post.weixue_allowlist, "weixue_allowlist")
    ws_server = sys.modules["core.websocket_server"].WebSocketServer
    assert ws_server._handle_auth.weixue_wrapped.__name__ == "_handle_auth"
    assert not hasattr(ws_server._handle_auth.weixue_wrapped, "weixue_wrapped")
