"""weixue_clock: the OTA response's server_time.tz carries the POSIX TZ of TZ in server/.env."""
import functools
import json
import sys
import types

import pytest
from aiohttp import web
from multidict import CIMultiDict

import weixue_clock
from weixue_clock import posix_tz
import weixue_patches
from tests.test_conversation_timing import stub_handler_modules, stub_listen_and_http

FOOTER_FILE = b"TZif2" + b"\0" * 40 + b"\nTZif2" + b"\0" * 20 + b"\nPST8PDT,M3.2.0,M11.1.0\n"


def _zoneinfo(tmp_path):
    zone = tmp_path / "America" / "Los_Angeles"
    zone.parent.mkdir()
    zone.write_bytes(FOOTER_FILE)
    (tmp_path / "Old").write_bytes(b"TZif\0" + b"\0" * 40)
    return str(tmp_path)


def test_posix_tz_from_tzif_footer(tmp_path):
    zi = _zoneinfo(tmp_path)
    assert weixue_clock.posix_tz("America/Los_Angeles", zi) == "PST8PDT,M3.2.0,M11.1.0"
    assert weixue_clock.posix_tz(":America/Los_Angeles", zi) == "PST8PDT,M3.2.0,M11.1.0"


def test_posix_tz_unavailable(tmp_path):
    zi = _zoneinfo(tmp_path)
    assert weixue_clock.posix_tz("Old", zi) is None  # TZif v1 has no footer
    assert weixue_clock.posix_tz("Nowhere/City", zi) is None
    assert weixue_clock.posix_tz("../../etc/passwd", zi) is None
    assert weixue_clock.posix_tz("", zi) is None


def test_posix_rule_passes_through(tmp_path):
    zi = _zoneinfo(tmp_path)
    assert weixue_clock.posix_tz("CET-1CEST,M3.5.0,M10.5.0/3", zi) == "CET-1CEST,M3.5.0,M10.5.0/3"
    assert weixue_clock.posix_tz("UTC0", zi) == "UTC0"


def test_posix_tz_defaults_to_weixue_tz(tmp_path, monkeypatch):
    monkeypatch.setenv("WEIXUE_TZ", "America/Los_Angeles")
    monkeypatch.setenv("TZ", "UTC")
    assert weixue_clock.posix_tz(zoneinfo=_zoneinfo(tmp_path)) == "PST8PDT,M3.2.0,M11.1.0"


def test_compose_default_tz_is_not_sent(tmp_path, monkeypatch):
    (tmp_path / "UTC").write_bytes(b"TZif2" + b"\0" * 40 + b"\nTZif2" + b"\0" * 20 + b"\nUTC0\n")
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("WEIXUE_TZ", "")
    assert weixue_clock.posix_tz(zoneinfo=str(tmp_path)) is None
    monkeypatch.delenv("WEIXUE_TZ")
    assert weixue_clock.posix_tz(zoneinfo=str(tmp_path)) is None
    monkeypatch.setenv("WEIXUE_TZ", "UTC")
    assert weixue_clock.posix_tz(zoneinfo=str(tmp_path)) == "UTC0"


def _ota(body, status=200):
    async def handle_post(self, request):
        return web.Response(text=json.dumps(body), content_type="application/json", status=status)

    return handle_post


async def test_ota_response_gains_tz():
    body = {"server_time": {"timestamp": 1791408605000, "timezone_offset": -420},
            "firmware": {"version": "2.5.1", "url": ""}}
    handle = weixue_clock.add_tz(_ota(body), "PST8PDT,M3.2.0,M11.1.0")
    response = await handle(None, types.SimpleNamespace())
    assert json.loads(response.text) == {
        "server_time": {"timestamp": 1791408605000, "timezone_offset": -420,
                        "tz": "PST8PDT,M3.2.0,M11.1.0"},
        "firmware": {"version": "2.5.1", "url": ""},
    }
    assert response.content_type == "application/json"


async def test_ota_response_unchanged_without_tz_or_server_time():
    body = {"server_time": {"timestamp": 1}}
    response = await weixue_clock.add_tz(_ota(body), None)(None, None)
    assert json.loads(response.text) == body
    error = {"success": False, "message": "request error."}
    response = await weixue_clock.add_tz(_ota(error), "UTC0")(None, None)
    assert json.loads(response.text) == error
    response = await weixue_clock.add_tz(_ota(body, status=500), "UTC0")(None, None)
    assert json.loads(response.text) == body


DUBLIN = "IST-1GMT0,M10.5.0,M3.5.0/1"
DEVICE = "7c:0c:5f:42:4b:fc"


async def test_allowlist_and_tz_both_wrap_ota_once(monkeypatch):
    connection = types.ModuleType("core.connection")

    class ConnectionHandler:
        _clean_response_garbage = staticmethod(lambda text: text)
        _extract_direct_answer_response = staticmethod(lambda arguments: "")

        async def handle_connection(self, ws):
            return None

    connection.ConnectionHandler = ConnectionHandler
    monkeypatch.setitem(sys.modules, "core.connection", connection)
    stub_handler_modules(monkeypatch)
    stub_listen_and_http(monkeypatch)
    ota = sys.modules["core.api.ota_handler"].OTAHandler
    upstream = _ota({"server_time": {"timestamp": 1}, "websocket": {"url": "ws://x", "token": ""}})
    ota.handle_post = upstream
    ota.auth_enable, ota.allowed_devices = True, {DEVICE}
    ota._add_cors_headers = lambda self, response: None
    monkeypatch.setenv("WEIXUE_TZ", DUBLIN)

    for _ in range(2):
        weixue_patches.apply()
        weixue_clock.apply()

    assert ota.handle_post.weixue_tz.weixue_allowlist is upstream

    def request(device_id):
        return types.SimpleNamespace(headers=CIMultiDict({"device-id": device_id}))

    refused = await ota.handle_post(ota(), request("aa:bb:cc:dd:ee:ff"))
    assert refused.status == 403
    assert json.loads(refused.text) == {"success": False, "message": "device not allowed"}
    listed = await ota.handle_post(ota(), request(DEVICE))
    assert listed.status == 200
    assert json.loads(listed.text)["server_time"] == {"timestamp": 1, "tz": DUBLIN}


async def _ota_server_time(monkeypatch, tmp_path, weixue_tz):
    zone = tmp_path / "Europe" / "Dublin"
    zone.parent.mkdir()
    zone.write_bytes(b"TZif2" + b"\0" * 40 + b"\nTZif2" + b"\0" * 20 + b"\n" + DUBLIN.encode() + b"\n")
    monkeypatch.setattr(weixue_clock, "posix_tz", functools.partial(posix_tz, zoneinfo=str(tmp_path)))
    ota = types.ModuleType("core.api.ota_handler")

    class OTAHandler:
        handle_post = _ota({"server_time": {"timestamp": 1, "timezone_offset": 60}})

    ota.OTAHandler = OTAHandler
    logger = types.ModuleType("config.logger")
    logger.setup_logging = lambda: types.SimpleNamespace(
        bind=lambda **_: types.SimpleNamespace(info=lambda _: None))
    monkeypatch.setitem(sys.modules, "core.api.ota_handler", ota)
    monkeypatch.setitem(sys.modules, "config.logger", logger)
    monkeypatch.setenv("TZ", "UTC")
    if weixue_tz is None:
        monkeypatch.delenv("WEIXUE_TZ", raising=False)
    else:
        monkeypatch.setenv("WEIXUE_TZ", weixue_tz)
    weixue_clock.apply()
    response = await OTAHandler.handle_post(OTAHandler(), None)
    return json.loads(response.text)["server_time"]


@pytest.mark.parametrize("weixue_tz", [None, ""])
async def test_unset_tz_sends_no_tz_field(monkeypatch, tmp_path, weixue_tz):
    assert await _ota_server_time(monkeypatch, tmp_path, weixue_tz) == {"timestamp": 1, "timezone_offset": 60}


async def test_dublin_tz_is_sent(monkeypatch, tmp_path):
    server_time = await _ota_server_time(monkeypatch, tmp_path, "Europe/Dublin")
    assert server_time == {"timestamp": 1, "timezone_offset": 60, "tz": DUBLIN}
