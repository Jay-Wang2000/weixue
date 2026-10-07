"""Upstream ConnectionHandler.chat() and its stream helpers, loaded from the vendored
core/connection.py source onto a stand-in connection (the module itself needs the full
server stack). Tool dispatch runs on a real event loop; _handle_function_result records."""
import ast
import asyncio
import json
import queue
import re
import threading
import uuid
from types import SimpleNamespace

from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
from core.utils.dialogue import Dialogue, Message

from tests.conftest import LOGGER, VENDOR_DIR

METHODS = ("chat", "_merge_tool_calls", "_extract_direct_answer_response", "_clean_response_garbage")
SYSTEM_ERROR = "SYSTEM ERROR"


def _load():
    tree = ast.parse((VENDOR_DIR / "core" / "connection.py").read_text(encoding="utf-8"))
    namespace = {
        "json": json,
        "re": re,
        "uuid": uuid,
        "asyncio": asyncio,
        "TAG": "core.connection",
        "Message": Message,
        "TTSMessageDTO": TTSMessageDTO,
        "SentenceType": SentenceType,
        "ContentType": ContentType,
        "textUtils": SimpleNamespace(),
        "enqueue_tool_report": lambda *_a, **_k: None,
        "get_system_error_response": lambda _config: SYSTEM_ERROR,
        "extract_json_from_string": lambda _s: None,
        "ActionResponse": SimpleNamespace,
        "Action": SimpleNamespace(ERROR="ERROR"),
    }
    body = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "DIRECT_ANSWER_TOOL" for t in node.targets
        ):
            body.append(node)
        if isinstance(node, ast.ClassDef) and node.name == "ConnectionHandler":
            methods = [n for n in node.body if isinstance(n, ast.FunctionDef) and n.name in METHODS]
            assert sorted(m.name for m in methods) == sorted(METHODS), "upstream chat helpers moved"
            body.append(ast.ClassDef(
                name="UpstreamChat", bases=[], keywords=[], body=methods, decorator_list=[]
            ))
    module = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    exec(compile(module, str(VENDOR_DIR / "core" / "connection.py"), "exec"), namespace)
    return namespace["UpstreamChat"], namespace["DIRECT_ANSWER_TOOL"]


UpstreamChat, DIRECT_ANSWER_TOOL = _load()


class Tools:
    def __init__(self, functions):
        self.functions = functions
        self.dispatched = []

    def get_functions(self):
        return list(self.functions)

    async def handle_llm_function_call(self, _conn, tool_call_data):
        self.dispatched.append(dict(tool_call_data))
        return SimpleNamespace(action="RESPONSE", result="ok", response="ok")


def make_connection(llm, functions, patch=None):
    """A stand-in ConnectionHandler running upstream chat() against llm. patch(cls) may
    replace methods on the class (as weixue_patches.apply does on ConnectionHandler)."""
    cls = type("Conn", (UpstreamChat,), {})
    if patch is not None:
        patch(cls)
    conn = cls()
    conn.logger = LOGGER
    conn.llm = llm
    conn.intent_type = "function_call"
    conn.func_handler = Tools(functions)
    conn.client_abort = False
    conn.memory = None
    conn.current_speaker = None
    conn.system_introduced_speakers = set()
    conn.config = {"voiceprint": {}, "tool_call_timeout": 5}
    conn.features = {"emoji": False}
    conn.session_id = "s"
    conn.sentence_id = None
    conn.dialogue = Dialogue()
    conn.dialogue.put(Message(role="system", content="SYSTEM"))
    stored = {}
    conn.tts = SimpleNamespace(
        tts_text_queue=queue.Queue(),
        store_tts_text=lambda sid, text: stored.__setitem__(sid, text),
        stored=stored,
    )
    conn.results = []
    conn._handle_function_result = lambda results, depth, streamed_text="": conn.results.extend(results)
    return conn


def run_chat(conn, query="hi"):
    """Runs conn.chat(query) with its event loop on a helper thread; returns the TTS messages."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    conn.loop = loop
    try:
        conn.chat(query)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()
    messages = []
    while True:
        try:
            messages.append(conn.tts.tts_text_queue.get_nowait())
        except queue.Empty:
            return messages


def spoken(messages):
    return "".join(
        m.content_detail for m in messages if m.content_type == ContentType.TEXT and m.content_detail
    )
