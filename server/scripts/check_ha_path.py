#!/usr/bin/env python3
"""One read-only Home Assistant call along the voice path, as the server makes it.

Usage (inside the server container, setup.sh):
  docker exec -i xiaozhi-esp32-server python - [--config PATH] [--entity ID] [--timeout S] < check_ha_path.py

Connects to the caller side (/mcp_endpoint/call/) of the `mcp_endpoint` in the server config,
lists the tools the bridge registered, and calls ha_get_state for --entity (default zone.home)
through endpoint, bridge and ha-mcp. Passes when Home Assistant answered: the entity's state,
or ha-mcp's ENTITY_NOT_FOUND. Prints one line, never a URL or token; exits 0 on pass, else 1.
"""
import argparse
import asyncio
import json
import sys

import websockets
import yaml

PROTOCOL_VERSION = "2024-11-05"
TOOL = "ha_get_state"
TOOL_ERROR_PREFIX = "Tool error: "


def caller_url(mcp_endpoint):
    return mcp_endpoint.replace("/mcp/", "/call/")


def answered(result):
    """True when a tools/call result carries Home Assistant's answer."""
    if not isinstance(result, dict):
        return False
    content = result.get("content")
    text = content[0].get("text") if isinstance(content, list) and content and isinstance(content[0], dict) else None
    if not isinstance(text, str) or result.get("isError") is True:
        return False
    if text.startswith(TOOL_ERROR_PREFIX):
        return "ENTITY_NOT_FOUND" in text
    return text.lstrip().startswith("{")


async def _rpc(ws, message):
    await ws.send(json.dumps(message))
    while True:
        reply = json.loads(await ws.recv())
        if isinstance(reply, dict) and reply.get("id") == message["id"]:
            return reply


async def probe(url, entity):
    async with websockets.connect(url, open_timeout=5) as ws:
        init = await _rpc(ws, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "weixue-check-ha-path"}},
        })
        if "result" not in init:
            return "initialize failed"
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}))
        tools = await _rpc(ws, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t.get("name") for t in (tools.get("result") or {}).get("tools") or [] if isinstance(t, dict)}
        if TOOL not in names:
            return f"no {TOOL} among {len(names)} routed tools"
        call = await _rpc(ws, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": TOOL, "arguments": {"entity_id": entity}},
        })
        if not answered(call.get("result")):
            return f"{TOOL} got no Home Assistant answer"
        return ""


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only Home Assistant call along the voice path.")
    parser.add_argument("--config", default="data/.config.yaml")
    parser.add_argument("--entity", default="zone.home")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)
    try:
        with open(args.config, encoding="utf-8") as f:
            endpoint = (yaml.safe_load(f) or {}).get("mcp_endpoint")
    except (OSError, yaml.YAMLError) as e:
        print(f"ha path: config unreadable ({type(e).__name__})")
        return 1
    if not isinstance(endpoint, str) or not endpoint.startswith("ws"):
        print("ha path: mcp_endpoint is not set")
        return 1
    try:
        problem = asyncio.run(asyncio.wait_for(probe(caller_url(endpoint), args.entity), args.timeout))
    except asyncio.TimeoutError:
        problem = f"no answer within {args.timeout:.0f}s"
    except (OSError, websockets.exceptions.WebSocketException, ValueError) as e:
        problem = f"endpoint connection failed ({type(e).__name__})"
    if problem:
        print(f"ha path: {problem}")
        return 1
    print(f"ha path ok: {TOOL} answered through endpoint, bridge and ha-mcp")
    return 0


if __name__ == "__main__":
    sys.exit(main())
