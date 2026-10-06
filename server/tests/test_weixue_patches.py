"""weixue_patches against upstream ConnectionHandler._clean_response_garbage, loaded from
the vendored core/connection.py source (the module itself needs the full server stack)."""
import ast
import re
import sys
import types

import pytest

import weixue_patches
from tests.conftest import VENDOR_DIR


def _upstream_clean():
    tree = ast.parse((VENDOR_DIR / "core" / "connection.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_clean_response_garbage":
            node.decorator_list = []
            namespace = {"re": re}
            exec(compile(ast.Module(body=[node], type_ignores=[]), "connection.py", "exec"), namespace)
            return namespace["_clean_response_garbage"]
    raise AssertionError("_clean_response_garbage not found in upstream core/connection.py")


UPSTREAM = _upstream_clean()
PATCHED = weixue_patches.keep_trailing_whitespace(UPSTREAM)


def _stream(clean, chunks):
    return "".join(clean(chunk) for chunk in chunks)


def test_upstream_drops_space_at_chunk_edges():
    assert _stream(UPSTREAM, ["Two plus ", "two is ", "four."]) == "Two plustwo isfour."


def test_mixed_english_and_chinese_chunks_keep_word_spaces():
    chunks = ["Two plus ", "two is ", "four. ", "二加二", "等于四。 ", "OK, ", "好的"]
    assert _stream(PATCHED, chunks) == "Two plus two is four. 二加二等于四。OK, 好的"


@pytest.mark.parametrize(
    "chunk",
    ["二加二等于四。 ", "打开客厅的灯 ", "你好\n", "好的！\n\n", "客厅灯已打开。", "  "],
)
def test_chinese_chunks_match_upstream(chunk):
    assert PATCHED(chunk) == UPSTREAM(chunk)


@pytest.mark.parametrize(
    "chunk, expected",
    [
        ('four."}', "four."),
        ('four."} ', "four."),
        ("four.\n\"}}\n", "four."),
        ("is\n", "is\n"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_garbage_still_removed(chunk, expected):
    assert PATCHED(chunk) == expected


def test_apply_patches_each_hook_once(monkeypatch):
    connection = types.ModuleType("core.connection")
    listen = types.ModuleType("core.handle.textHandler.listenMessageHandler")
    http_server = types.ModuleType("core.http_server")

    async def handle_connection(self, ws):
        return None

    async def handle(self, conn, msg_json):
        return None

    class ConnectionHandler:
        _clean_response_garbage = staticmethod(UPSTREAM)

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

    weixue_patches.apply()
    weixue_patches.apply()

    patched = ConnectionHandler._clean_response_garbage
    assert patched.weixue_wrapped is UPSTREAM
    assert patched("two is ") == "two is "
    assert ConnectionHandler()._clean_response_garbage("two is ") == "two is "
    assert ConnectionHandler.handle_connection.weixue_wrapped is handle_connection
    assert ListenTextMessageHandler.handle.weixue_wrapped is handle
    assert isinstance(http_server.web, weixue_patches.WebWithRoutes)
    assert http_server.web._web is web
