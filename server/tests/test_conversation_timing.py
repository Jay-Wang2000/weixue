"""Follow-up window and wake-word patches in weixue_patches against upstream
no_voice_close_connect and remove_punctuation_and_length, loaded from the vendored source."""
import ast
import sys
import time
import types

import pytest

import weixue_patches
from tests.conftest import LOGGER, VENDOR_DIR

WINDOW_S = 8


def _upstream_function(relpath, name, namespace):
    tree = ast.parse((VENDOR_DIR / relpath).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            exec(compile(ast.Module(body=[node], type_ignores=[]), relpath, "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"{name} not found in upstream {relpath}")


FAREWELLS = []


async def _record_farewell(_conn, text):
    FAREWELLS.append(text)


UPSTREAM_NO_VOICE = _upstream_function(
    "core/handle/receiveAudioHandle.py",
    "no_voice_close_connect",
    {"time": time, "startToChat": _record_farewell, "TAG": "receiveAudioHandle"},
)
REMOVE_PUNCTUATION = _upstream_function(
    "core/utils/util.py", "remove_punctuation_and_length", {}
)
PATCHED_NO_VOICE = weixue_patches.hold_idle_timer_while_replying(UPSTREAM_NO_VOICE)
CHECK_WAKEUP_WORDS = weixue_patches.ignore_bare_wake_word(REMOVE_PUNCTUATION, LOGGER)


class Conn:
    def __init__(self, silent_for_s, speaking=False, end_prompt=False):
        self.config = {
            "close_connection_no_voice_time": WINDOW_S,
            "end_prompt": {"enable": end_prompt, "prompt": "farewell"},
            "wakeup_words": ["你好小智", "嘿你好呀", "Jarvis"],
        }
        self.last_activity_time = (time.time() - silent_for_s) * 1000
        self.client_is_speaking = speaking
        self.close_after_chat = False
        self.client_abort = True
        self.closed = 0
        self.logger = LOGGER

    async def close(self):
        self.closed += 1


@pytest.fixture(autouse=True)
def clear_farewells():
    FAREWELLS.clear()


async def test_upstream_closes_during_a_long_turn():
    conn = Conn(silent_for_s=WINDOW_S + 4, speaking=True)
    await UPSTREAM_NO_VOICE(conn, False)
    assert conn.closed == 1


async def test_long_turn_keeps_the_session_open():
    conn = Conn(silent_for_s=WINDOW_S + 30, speaking=True)
    await PATCHED_NO_VOICE(conn, False)
    assert conn.closed == 0
    assert time.time() * 1000 - conn.last_activity_time < 1000


async def test_window_counts_from_the_end_of_the_reply():
    conn = Conn(silent_for_s=WINDOW_S + 30, speaking=True)
    await PATCHED_NO_VOICE(conn, False)
    conn.client_is_speaking = False
    conn.last_activity_time -= (WINDOW_S - 1) * 1000
    await PATCHED_NO_VOICE(conn, False)
    assert conn.closed == 0
    conn.last_activity_time -= 2 * 1000
    await PATCHED_NO_VOICE(conn, False)
    assert conn.closed == 1


async def test_silence_after_the_window_closes_once_without_farewell():
    conn = Conn(silent_for_s=WINDOW_S + 1)
    await PATCHED_NO_VOICE(conn, False)
    await PATCHED_NO_VOICE(conn, False)
    assert conn.closed == 1
    assert conn.close_after_chat is True
    assert FAREWELLS == []


async def test_voice_inside_the_window_restarts_it():
    conn = Conn(silent_for_s=WINDOW_S - 1)
    await PATCHED_NO_VOICE(conn, True)
    conn.last_activity_time -= (WINDOW_S - 1) * 1000
    await PATCHED_NO_VOICE(conn, False)
    assert conn.closed == 0


async def test_enabled_end_prompt_still_reaches_upstream():
    conn = Conn(silent_for_s=WINDOW_S + 1, end_prompt=True)
    await PATCHED_NO_VOICE(conn, False)
    assert conn.closed == 0
    assert FAREWELLS == ["farewell"]


@pytest.mark.parametrize("text", ["Jarvis", "Jarvis.", "jarvis?", " JARVIS! ", "嘿，你好呀", "你好小智。"])
async def test_bare_wake_word_is_consumed(text, log_records):
    assert await CHECK_WAKEUP_WORDS(Conn(0), text) is True
    assert any("wake word transcript ignored" in message for _, message in log_records)


@pytest.mark.parametrize("text", ["Jarvis, what time is it?", "Hey Jarvis", "What time is it?", "", "。"])
async def test_other_transcripts_go_to_the_llm(text):
    assert await CHECK_WAKEUP_WORDS(Conn(0), text) is False


def stub_handler_modules(monkeypatch):
    """Stubs the upstream modules weixue_patches.apply() patches or imports besides
    core.connection, core.http_server, core.auth and the listen handler."""
    receive = types.ModuleType("core.handle.receiveAudioHandle")
    receive.no_voice_close_connect = UPSTREAM_NO_VOICE
    intent = types.ModuleType("core.handle.intentHandler")

    async def upstream_check_wakeup_words(_conn, _text):
        return False

    intent.checkWakeupWords = upstream_check_wakeup_words
    util = types.ModuleType("core.utils.util")
    util.remove_punctuation_and_length = REMOVE_PUNCTUATION

    async def audio_to_data(_path, is_opus=True, use_cache=True):
        return []

    util.audio_to_data = audio_to_data
    send = types.ModuleType("core.handle.sendAudioHandle")
    send.send_tts_message = None
    send.sendAudioMessage = None
    send.SentenceType = types.SimpleNamespace(FIRST="FIRST", LAST="LAST")
    silero = types.ModuleType("core.providers.vad.silero")

    class VADProvider:
        def is_vad(self, conn, pcm_frame):
            return False

    silero.VADProvider = VADProvider
    mcp = types.ModuleType("core.handle.textHandler.mcpMessageHandler")

    class McpTextMessageHandler:
        async def handle(self, conn, msg_json):
            return "upstream"

    mcp.McpTextMessageHandler = McpTextMessageHandler
    ota = types.ModuleType("core.api.ota_handler")

    class OTAHandler:
        async def handle_post(self, request):
            return "upstream"

    ota.OTAHandler = OTAHandler
    websocket_server = types.ModuleType("core.websocket_server")

    class WebSocketServer:
        async def _handle_auth(self, websocket):
            return None

    websocket_server.WebSocketServer = WebSocketServer
    for module in (receive, intent, util, send, silero, mcp, ota, websocket_server):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    return receive, intent, VADProvider


def stub_listen_and_http(monkeypatch):
    listen = types.ModuleType("core.handle.textHandler.listenMessageHandler")

    class ListenTextMessageHandler:
        async def handle(self, conn, msg_json):
            return "upstream"

    listen.ListenTextMessageHandler = ListenTextMessageHandler
    http_server = types.ModuleType("core.http_server")
    http_server.web = types.SimpleNamespace(Application=lambda: types.SimpleNamespace(routes=[]))
    monkeypatch.setitem(sys.modules, listen.__name__, listen)
    monkeypatch.setitem(sys.modules, http_server.__name__, http_server)
    return ListenTextMessageHandler


async def test_apply_patches_handlers_once(monkeypatch):
    connection = types.ModuleType("core.connection")

    class ConnectionHandler:
        _clean_response_garbage = staticmethod(lambda text: text)
        _extract_direct_answer_response = staticmethod(lambda arguments: "")

        async def handle_connection(self, ws):
            return None

        def __init__(self):
            self.just_woken_up = False

    connection.ConnectionHandler = ConnectionHandler
    monkeypatch.setitem(sys.modules, "core.connection", connection)
    receive, intent, vad = stub_handler_modules(monkeypatch)
    listen_handler = stub_listen_and_http(monkeypatch)

    weixue_patches.apply()
    weixue_patches.apply()

    assert receive.no_voice_close_connect.weixue_wrapped is UPSTREAM_NO_VOICE
    assert intent.checkWakeupWords.weixue_replacement is True
    assert await intent.checkWakeupWords(Conn(0), "Jarvis") is True
    conn = Conn(silent_for_s=WINDOW_S + 30, speaking=True)
    await receive.no_voice_close_connect(conn, False)
    assert conn.closed == 0
    handler = ConnectionHandler()
    handler.just_woken_up = True
    assert handler.just_woken_up is False
    assert vad.is_vad.weixue_wrapped.__qualname__ == "stub_handler_modules.<locals>.VADProvider.is_vad"
    wake_wrapper = listen_handler.handle.weixue_wrapped
    assert wake_wrapper.weixue_wrapped.__qualname__ == "stub_listen_and_http.<locals>.ListenTextMessageHandler.handle"
