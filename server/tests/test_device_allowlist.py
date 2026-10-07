"""Strict device allow-list (weixue_patches.refuse_unlisted_ota / refuse_unlisted_websocket)
run against upstream OTAHandler.handle_post and WebSocketServer._handle_connection."""
import asyncio
import importlib
import json
import sys
import types

import pytest
from aiohttp import web
from multidict import CIMultiDict
from websockets.datastructures import Headers

import weixue_patches
from core.auth import AuthenticationError, AuthManager
from tests.conftest import LOGGER

DEVICE = "7c:0c:5f:42:4b:fc"
OTHER = "aa:bb:cc:dd:ee:ff"
CLIENT = "client-1"
AUTH_KEY = "test-auth-key"


def server_config(enabled=True, devices=(DEVICE,)):
    return {
        "server": {
            "auth_key": AUTH_KEY,
            "auth": {"enabled": enabled, "allowed_devices": list(devices)},
            "port": 8000,
            "http_port": 8003,
            "websocket": "ws://192.0.2.1:8000/xiaozhi/v1/",
            "timezone_offset": 1,
        }
    }


# ------------------------------------------------------------------ OTA


class Request:
    method = "POST"

    def __init__(self, headers):
        self.headers = CIMultiDict(headers)

    async def text(self):
        return json.dumps({"application": {"version": "1.0.0"}, "board": {"type": "test"}})


@pytest.fixture
def ota(monkeypatch):
    ota_handler = importlib.import_module("core.api.ota_handler")
    monkeypatch.setattr(ota_handler, "get_local_ip", lambda: "192.0.2.1")

    def post(device_id=None, patched=True, **config):
        handler = ota_handler.OTAHandler(server_config(**config))
        handle = ota_handler.OTAHandler.handle_post
        if patched:
            handle = weixue_patches.refuse_unlisted_ota(handle, web, LOGGER)
        headers = {"client-id": CLIENT}
        if device_id is not None:
            headers["device-id"] = device_id
        response = asyncio.run(handle(handler, Request(headers)))
        return response.status, json.loads(response.text)

    return post


def test_ota_upstream_issues_a_token_to_an_unlisted_device(ota):
    status, body = ota(OTHER, patched=False)
    assert status == 200 and body["websocket"]["token"]


def test_ota_listed_device_gets_websocket_without_token(ota):
    status, body = ota(DEVICE)
    assert status == 200
    assert body["websocket"] == {"url": "ws://192.0.2.1:8000/xiaozhi/v1/", "token": ""}


@pytest.mark.parametrize("device_id", [OTHER, DEVICE.upper(), "", None])
def test_ota_unlisted_device_gets_403_without_websocket(ota, device_id, log_records):
    status, body = ota(device_id)
    assert status == 403
    assert body == {"success": False, "message": "device not allowed"}
    assert any(level == "warning" and "OTA refused" in message for level, message in log_records)


def test_ota_with_auth_disabled_is_upstream(ota):
    status, body = ota(OTHER, enabled=False)
    assert status == 200 and body["websocket"]["token"] == ""


# ------------------------------------------------------------------ WebSocket


class FakeWebSocket:
    def __init__(self, headers, path="/xiaozhi/v1/"):
        self.request = types.SimpleNamespace(headers=Headers(headers), path=path)
        self.sent = []
        self.closed = False

    async def send(self, message):
        self.sent.append(message)

    async def close(self):
        self.closed = True


@pytest.fixture
def ws(monkeypatch):
    sessions = []

    class ConnectionHandler:
        def __init__(self, *args):
            pass

        async def handle_connection(self, websocket):
            sessions.append(websocket)

    stubs = {
        "core.connection": {"ConnectionHandler": ConnectionHandler},
        "config.config_loader": {"get_config_from_api_async": None},
        "core.utils.modules_initialize": {"initialize_modules": lambda *a, **k: {}},
    }
    for name, attrs in stubs.items():
        module = types.ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, "core.websocket_server", raising=False)
    websocket_server = importlib.import_module("core.websocket_server")
    monkeypatch.delitem(sys.modules, "core.websocket_server")
    cls = websocket_server.WebSocketServer
    upstream_auth = cls._handle_auth

    def connect(headers, path="/xiaozhi/v1/", patched=True, enabled=True):
        auth = weixue_patches.refuse_unlisted_websocket(upstream_auth, AuthenticationError, LOGGER)
        monkeypatch.setattr(cls, "_handle_auth", auth if patched else upstream_auth)
        server = object.__new__(cls)
        server.config, server.logger = server_config(), LOGGER
        server._vad = server._asr = server._llm = server._memory = server._intent = None
        server.auth_enable = enabled
        server.allowed_devices = {DEVICE}
        server.auth = AuthManager(secret_key=AUTH_KEY)
        websocket = FakeWebSocket(headers, path)
        sessions.clear()
        asyncio.run(server._handle_connection(websocket))
        return websocket, list(sessions)

    return connect


def token_for(device_id):
    return "Bearer " + AuthManager(secret_key=AUTH_KEY).generate_token(CLIENT, device_id)


def test_ws_upstream_admits_an_unlisted_device_with_an_ota_token(ws):
    websocket, sessions = ws({"device-id": OTHER, "client-id": CLIENT, "authorization": token_for(OTHER)}, patched=False)
    assert sessions == [websocket]


@pytest.mark.parametrize("authorization", [None, token_for(DEVICE)])
def test_ws_listed_device_connects_with_or_without_token(ws, authorization):
    headers = {"device-id": DEVICE, "client-id": CLIENT}
    if authorization:
        headers["authorization"] = authorization
    websocket, sessions = ws(headers)
    assert sessions == [websocket] and websocket.sent == []


@pytest.mark.parametrize("authorization", [None, token_for(OTHER), "Bearer forged"])
def test_ws_unlisted_device_is_refused_whatever_its_token(ws, authorization, log_records):
    headers = {"device-id": OTHER, "client-id": CLIENT}
    if authorization:
        headers["authorization"] = authorization
    websocket, sessions = ws(headers)
    assert sessions == [] and websocket.sent == ["认证失败"] and websocket.closed
    assert any(level == "warning" and "WebSocket refused" in message for level, message in log_records)


def test_ws_unlisted_device_in_query_parameters_is_refused(ws):
    path = f"/xiaozhi/v1/?device-id={OTHER}&client-id={CLIENT}&authorization={token_for(OTHER).replace(' ', '%20')}"
    websocket, sessions = ws({}, path=path)
    assert sessions == [] and websocket.sent == ["认证失败"]


def test_ws_without_device_id_still_gets_the_banner(ws):
    websocket, sessions = ws({})
    assert sessions == [] and websocket.sent[0].startswith("端口正常") and websocket.closed


def test_ws_with_auth_disabled_is_upstream(ws):
    websocket, sessions = ws({"device-id": OTHER, "client-id": CLIENT}, enabled=False)
    assert sessions == [websocket]
