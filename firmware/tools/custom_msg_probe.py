"""OTA + WebSocket proxy that injects test frames after the server hello.

The device's OTA URL points here; OTA responses are forwarded with websocket.url
rewritten to this proxy, and each WebSocket session is relayed to the original
websocket.url. After the first server `hello`, the proxy sends a valid `custom`
message, a `custom` message with a non-object payload and an unknown `type`.
Never logs tokens or forwarded headers.
"""
import argparse
import asyncio
import json
import logging

import aiohttp
from aiohttp import web

FORWARD_HEADERS = ("authorization", "protocol-version", "device-id", "client-id",
                   "user-agent", "accept-language", "activation-version", "content-type")

PROBE_FRAMES = (
    {"type": "custom", "payload": {"probe": "weixue", "check": "valid"}},
    {"type": "custom", "payload": "not-an-object"},
    {"type": "weixue_probe_unknown"},
)

log = logging.getLogger("probe")


def frame_type(data):
    try:
        msg = json.loads(data)
    except ValueError:
        return "non-json"
    return msg.get("type") if isinstance(msg, dict) else "non-object"


def pick(headers):
    return {k: v for k, v in headers.items() if k.lower() in FORWARD_HEADERS}


async def ota(request):
    app = request.app
    body = await request.read()
    async with app["http"].request(request.method, app["upstream_ota"], data=body or None,
                                   headers=pick(request.headers)) as r:
        raw = await r.read()
        status = r.status
    if status != 200:
        log.info("ota %s upstream status %s", request.method, status)
        return web.Response(status=status, body=raw)
    data = json.loads(raw)
    ws = data.get("websocket")
    if isinstance(ws, dict) and ws.get("url"):
        app["upstream_ws"] = ws["url"]
        ws["url"] = app["public_ws"]
    log.info("ota %s device=%s keys=%s websocket.url -> %s", request.method,
             request.headers.get("Device-Id", "?"), sorted(data), app["public_ws"])
    return web.json_response(data)


async def relay(request):
    app = request.app
    if not app.get("upstream_ws"):
        return web.Response(status=503, text="no OTA seen yet")
    down = web.WebSocketResponse(max_msg_size=0)
    await down.prepare(request)
    headers = pick(request.headers)
    headers.pop("content-type", None)
    device = request.headers.get("Device-Id", "?")
    log.info("ws open device=%s", device)
    async with app["http"].ws_connect(app["upstream_ws"], headers=headers, max_msg_size=0) as up:
        injected = False

        async def device_to_server():
            async for m in down:
                if m.type == aiohttp.WSMsgType.TEXT:
                    log.info("device -> %s", frame_type(m.data))
                    await up.send_str(m.data)
                elif m.type == aiohttp.WSMsgType.BINARY:
                    await up.send_bytes(m.data)
            await up.close()

        async def server_to_device():
            nonlocal injected
            async for m in up:
                if m.type == aiohttp.WSMsgType.TEXT:
                    t = frame_type(m.data)
                    log.info("server -> %s", t)
                    await down.send_str(m.data)
                    if t == "hello" and not injected:
                        injected = True
                        for frame in PROBE_FRAMES:
                            await down.send_str(json.dumps(frame))
                            log.info("probe -> %s", json.dumps(frame))
                elif m.type == aiohttp.WSMsgType.BINARY:
                    await down.send_bytes(m.data)
            await down.close()

        results = await asyncio.gather(device_to_server(), server_to_device(), return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            log.warning("relay task failed: %r", r)
    log.info("ws closed device=%s", device)
    return down


async def make_app(args):
    app = web.Application()
    app["upstream_ota"] = args.upstream_ota
    app["public_ws"] = f"ws://{args.public_host}:{args.port}/xiaozhi/v1/"

    async def session(app):
        app["http"] = aiohttp.ClientSession()
        yield
        await app["http"].close()

    app.cleanup_ctx.append(session)
    app.add_routes([web.get("/xiaozhi/ota/", ota), web.post("/xiaozhi/ota/", ota),
                    web.get("/xiaozhi/v1/", relay)])
    return app


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--upstream-ota", default="http://192.168.8.220:8003/xiaozhi/ota/")
    p.add_argument("--public-host", default="192.168.8.220")
    p.add_argument("--port", type=int, default=8013)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    web.run_app(make_app(args), port=args.port, print=None)


if __name__ == "__main__":
    main()
