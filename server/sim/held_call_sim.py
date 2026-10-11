#!/usr/bin/env python3
"""Held-call sim: agent-service as service.py builds it (HaMcpClient from settings), the fake ha-mcp of
server/agent-service/tests over real TCP holding wait_seconds=20, and a scripted model calling a held
ha_call_service; the card is approved once actionable. Prints the approval and card timeline. Run with the
agent-service test venv:

    python server/sim/held_call_sim.py

SIM_SERVICE_ROOT selects another server/agent-service tree (default: this checkout's)."""
import asyncio
import json
import os
import socket
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(os.environ.get("SIM_SERVICE_ROOT") or Path(__file__).resolve().parents[1] / "agent-service")
sys.path[:0] = [str(ROOT), str(ROOT.parent), str(ROOT / "tests")]

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from fake_api import FakeApi  # noqa: E402
from fake_hamcp import FakeHaMcp  # noqa: E402
from weixue_agent.api import Api  # noqa: E402
from weixue_agent.config import load_settings  # noqa: E402
from weixue_agent.service import Service  # noqa: E402

TOKEN = "s" * 40
DEVICE = "sim-device-01"
PLUG = {"domain": "switch", "service": "turn_on", "entity_id": "switch.working_desk_plug"}
T0 = time.monotonic()


def say(what, **kv):
    print(f"t={time.monotonic() - T0:6.2f}s {what} " + " ".join(f"{k}={v}" for k, v in kv.items()), flush=True)


async def script(turn):
    if turn.user_text.startswith("[event approval"):
        say("model got follow-up", text=json.dumps(turn.user_text[:90]))
        await turn.text("Done.")
        return
    await turn.text("Turning it on.")
    result = await turn.tool("ha_call_service", PLUG)
    say("model got tool result", text=json.dumps(result[:60]))
    await turn.text("I need your OK.")


async def main():
    fake = FakeHaMcp(wait_seconds=20)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    server = uvicorn.Server(uvicorn.Config(fake.app("/private_sim"), log_level="warning", lifespan="off",
                                           timeout_graceful_shutdown=1))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/private_sim"
    data = tempfile.mkdtemp(prefix="sim-held-")
    settings = load_settings({"AGENT_SERVICE_TOKEN": TOKEN, "HA_MCP_URL": url, "AGENT_DATA_DIR": data,
                              "ANTHROPIC_API_KEY": "sk-ant-sim-key-0000000000", "AGENT_MCP_ENDPOINT_HOST": ""})
    api = FakeApi(script)
    service = Service(settings, client=api.client())
    await service.ha.refresh()
    await service.start(background=False)
    say("service up", wait_seconds=service.ha.wait_seconds,
        read_timeout_s=getattr(settings, "ha_mcp_read_timeout_s", "n/a (main)"))
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=Api(service).app()), base_url="http://agent",
                             headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60)
    global T0
    T0 = time.monotonic()
    resp = await http.post("/v1/chats/current/messages", json={"device_id": DEVICE, "text": "turn on the desk plug"},
                           headers={"Idempotency-Key": str(uuid.uuid4()), "Accept": "text/event-stream"})
    say("reply stream closed", status=resp.status_code)
    seen = {}
    deadline = time.monotonic() + 40
    approved = False
    while time.monotonic() < deadline:
        row = service.store.one("SELECT * FROM approvals WHERE tool = 'ha_call_service'")
        card = service.store.one("SELECT * FROM interactions WHERE source = 'ha_mcp'")
        key = (row and row["state"], row and row["token_source"], card and card["state"],
               card and card["voice_can_approve"])
        if key not in seen.values():
            seen[time.monotonic()] = key
            say("approval", state=key[0], token_source=key[1], card=key[2], voice_can_approve=key[3])
        if card and card["state"] == "pending" and not approved:
            approved = True
            r = await http.post(f"/v1/chats/{card['chat_id']}/approve",
                                json={"interaction_id": card["interaction_id"], "decision_id": str(uuid.uuid4()),
                                      "via": "panel"})
            say("approve posted", status=r.status_code)
        if row and row["state"] in ("executed", "failed", "unknown", "denied", "expired"):
            say("terminal", state=row["state"], result=json.dumps((row["result_text"] or "")[:60]))
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(1.0)
    say("fake ha-mcp", calls=len(fake.calls), executed=len(fake.executed),
        decisions=json.dumps([d["decision"] for d in fake.decisions]))
    await http.aclose()
    await service.stop()
    api.close()
    fake.elapse_wait()
    server.should_exit = True
    await serving


asyncio.run(main())
