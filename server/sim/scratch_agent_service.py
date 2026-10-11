#!/usr/bin/env python3
"""Scratch agent-service for gateway recovery sim rounds: the real service (outbox, interactions,
chat index, HTTP API) on a temporary database, with the scripted Messages API and the fake
ha-mcp of server/agent-service/tests in place of Anthropic and Home Assistant, and fault
injection in front of the API. Run with the agent-service test venv:

    SIM_TOKEN=<32+ chars> python server/sim/scratch_agent_service.py --port 18810 --data <tmp dir>

SIM_SERVICE_ROOT selects another server/agent-service tree (default: this checkout's).

Control routes (same bearer token):
  POST /sim/faults  {"path": <regex>, "status": 503, "count": n, "after": bool}  next n matching requests
                    answer status; "after": the request is handled first and its answer replaced
  POST /sim/ask     {"device_id", "question", "options", "origin"?, "chat_id"?}  a chat turn (origin kind, default
                    none; chat default current) that leaves a question card
  POST /sim/enqueue {"device_id", "tier", "slot", "payload", "speak", "task_id", "chat_id"}  outbox.enqueue
  GET  /sim/rows?delivery_id=  the deliveries row and its delivery_acks; ?interaction_id= that row

A message "tools:<name>,<name>,..." is a turn that says one sentence, then uses each tool 1.5 s apart
(web_search, web_fetch and tool_search_* as server tools, others as client tools) and says "Done."
"""
import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(os.environ.get("SIM_SERVICE_ROOT") or Path(__file__).resolve().parents[1] / "agent-service")
sys.path[:0] = [str(ROOT), str(ROOT.parent), str(ROOT / "tests")]

import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Mount, Route  # noqa: E402

from fake_api import FakeApi  # noqa: E402
from fake_hamcp import FakeHaMcp  # noqa: E402
import httpx  # noqa: E402
from weixue_agent import config as service_config, deliveries, interactions, turns  # noqa: E402
from weixue_agent.api import Api  # noqa: E402
from weixue_agent.config import load_settings  # noqa: E402
from weixue_agent.service import Service  # noqa: E402
from weixue_hamcp import HaMcpClient  # noqa: E402

HA_URL = "http://ha.test:9584/private_sim"
TOOL_ARGS = {"ha_get_state": {"entity_id": "light.kitchen"}, "ha_search": {"query": "kitchen"},
             "ha_call_service": {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"}}


async def script(turn):
    if turn.user_text.startswith("ask:"):
        _, question, options = turn.user_text.split(":", 2)
        answer = await turn.ask(question, options.split("|"))
        await turn.text(f"Okay: {answer}.")
        return
    if turn.user_text.startswith("tools:"):
        await turn.text("Let me look.")
        for name in turn.user_text[len("tools:"):].split(","):
            await asyncio.sleep(1.5)
            if name in ("web_search", "web_fetch") or name.startswith("tool_search"):
                turn.server_tool(name, "sim")
            else:
                await turn.tool(name, TOOL_ARGS.get(name, {}))
        await asyncio.sleep(1.5)
    await turn.text("Done.")


class Faults:
    def __init__(self, app):
        self.app = app
        self.rules = []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith("/sim/"):
            return await self.app(scope, receive, send)
        rule = next((r for r in self.rules if r["count"] > 0 and re.search(r["path"], scope["path"])), None)
        if rule is None:
            return await self.app(scope, receive, send)
        rule["count"] -= 1
        print(json.dumps({"fault": scope["path"], "status": rule["status"], "after": rule["after"]}), flush=True)
        if rule["after"]:
            async def swallow(message):
                return None
            await self.app(scope, receive, swallow)
        response = JSONResponse({"error": {"code": "sim_fault"}}, status_code=rule["status"])
        await response(scope, receive, send)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18810)
    ap.add_argument("--data", required=True)
    ap.add_argument("--lease-s", type=float, default=3, help="offer lease (DELIVERY_LEASE_S)")
    ap.add_argument("--reply-hold-s", type=float, help="REPLY_HOLD_S (default: the service's)")
    a = ap.parse_args()
    if a.reply_hold_s is not None:
        turns.REPLY_HOLD_S = a.reply_hold_s
    token = os.environ["SIM_TOKEN"]
    deliveries.DELIVERY_LEASE_S = a.lease_s
    service_config.DELIVERY_LEASE_S = a.lease_s
    interactions.VOICE_WINDOW_S = 120
    settings = load_settings({"AGENT_SERVICE_TOKEN": token, "HA_MCP_URL": HA_URL, "AGENT_DATA_DIR": a.data,
                              "ANTHROPIC_API_KEY": "sk-ant-sim-key-0000000000", "TZ": "Europe/Dublin",
                              "AGENT_MCP_ENDPOINT_HOST": ""})
    fake_api = FakeApi(script)
    fake_ha = FakeHaMcp(wait_seconds=2.0)
    service = Service(settings, client=fake_api.client(),
                      ha_client=HaMcpClient(HA_URL, transport=httpx.ASGITransport(app=fake_ha.app("/private_sim"))))
    api = Api(service)
    faults = Faults(api.app())

    def authorized(request):
        return request.headers.get("authorization") == f"Bearer {token}"

    async def set_faults(request: Request):
        if not authorized(request):
            return JSONResponse({}, status_code=401)
        body = await request.json()
        faults.rules.append({"path": body["path"], "status": int(body.get("status", 503)),
                             "count": int(body.get("count", 1)), "after": bool(body.get("after"))})
        return JSONResponse({"rules": faults.rules})

    async def clear_faults(request: Request):
        faults.rules.clear()
        return JSONResponse({"rules": []})

    async def ask(request: Request):
        if not authorized(request):
            return JSONResponse({}, status_code=401)
        body = await request.json()
        transport = httpx.ASGITransport(app=api.app())
        async with httpx.AsyncClient(transport=transport, base_url="http://agent",
                                     headers={"Authorization": f"Bearer {token}"}, timeout=30) as http:
            message = {"device_id": body["device_id"], "text": f"ask:{body['question']}:{'|'.join(body['options'])}"}
            if body.get("origin"):
                message["origin"] = {"kind": body["origin"]}
            resp = await http.post(f"/v1/chats/{body.get('chat_id') or 'current'}/messages",
                                   headers={"Idempotency-Key": str(uuid.uuid4())}, json=message)
        await asyncio.sleep(1.0)
        row = service.store.one("SELECT * FROM interactions ORDER BY created_at DESC LIMIT 1")
        return JSONResponse({"status": resp.status_code, "interaction_id": row and row["interaction_id"],
                             "chat_id": row and row["chat_id"], "task_id": row and row["task_id"]})

    async def enqueue(request: Request):
        if not authorized(request):
            return JSONResponse({}, status_code=401)
        b = await request.json()
        did = service.outbox.enqueue(b["device_id"], b.get("kind", "agent"), b["tier"], b["slot"], b["payload"],
                                     task_id=b.get("task_id"), chat_id=b.get("chat_id"), speak=b.get("speak"),
                                     lang=b.get("lang", "en"), ttl_s=b.get("ttl_s"))
        return JSONResponse({"delivery_id": did})

    async def rows(request: Request):
        if not authorized(request):
            return JSONResponse({}, status_code=401)
        q = request.query_params
        if "interaction_id" in q:
            return JSONResponse({"interaction": service.store.interaction(q["interaction_id"]),
                                 "decisions": service.store.all("SELECT decision_id, kind FROM decisions WHERE target_id = ?",
                                                               (q["interaction_id"],))})
        did = q["delivery_id"]
        acks = service.store.all("SELECT outcome, reason, sentence_id, detail FROM delivery_acks WHERE delivery_id = ? "
                                 "ORDER BY id", (did,))
        return JSONResponse({"delivery": service.store.delivery(did), "acks": acks})

    async def lifespan(app):
        await service.ha.refresh()
        await service.start(background=False)
        yield
        await service.stop()

    import contextlib
    app = Starlette(routes=[
        Route("/sim/faults", set_faults, methods=["POST"]),
        Route("/sim/faults/clear", clear_faults, methods=["POST"]),
        Route("/sim/ask", ask, methods=["POST"]),
        Route("/sim/enqueue", enqueue, methods=["POST"]),
        Route("/sim/rows", rows, methods=["GET"]),
        Mount("/", app=faults),
    ], lifespan=contextlib.asynccontextmanager(lifespan))
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
