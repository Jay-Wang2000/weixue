#!/usr/bin/env python3
"""Chat switching end to end (feat-ui-sessions acceptance) on the sim stack
(server/sim/docker-compose.agent.yml): a v1.2 simulated device (agent_sim_device.py) creates chats
with `new_chat`, switches among them with `switch_chat` and after every switch asks a follow-up by
voice (proactive_sim_device.py, scripted ASR). A follow-up passes when the provider's
`turn_latency` line names the selected chat and the selected chat's agent-service events hold an
`accepted` event with that turn's trace id (no other chat does). --restart CONTAINER restarts the
agent-service container afterwards: the chat index and the current chat must be unchanged and
reach the device again; --restart-gateway then restarts --container and the list must come back. Prints one PASS/FAIL line per check; exit status 1 on any FAIL.

  agent_chats_e2e.py --base http://127.0.0.1:18203 --ws ws://127.0.0.1:18200/xiaozhi/v1/ \\
      --device aa:bb:cc:dd:ee:01 --container weixue-agent-sim-server --transcripts server/sim/data/sim_transcripts.txt \\
      --clip server/tests/assets/en-US_turn_on_the_living_room_lights.wav [--restart weixue-agent-sim-service]

Service calls run inside --container with its AGENT_SERVICE_URL / AGENT_SERVICE_TOKEN; the token is
never printed."""
import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import types
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_sim_device  # noqa: E402

TOPICS = ["apples", "bicycles", "castles", "dolphins", "engines", "forests", "glaciers"]
SWITCH_ORDER = [1, 3, 0, 4, 2, 1, 4, 0, 3, 2]
LIST_WAIT_S = 3.0
SERVICE_GET = (
    "import json,os,sys,urllib.request\n"
    "r=urllib.request.Request(os.environ['AGENT_SERVICE_URL'].rstrip('/')+sys.argv[1],"
    "headers={'Authorization':'Bearer '+os.environ['AGENT_SERVICE_TOKEN']})\n"
    "print(urllib.request.urlopen(r,timeout=10).read().decode())\n"
)

EVENTS_GET = (
    "import json,os,sys,urllib.request\n"
    "r=urllib.request.Request(os.environ['AGENT_SERVICE_URL'].rstrip('/')+sys.argv[1],"
    "headers={'Authorization':'Bearer '+os.environ['AGENT_SERVICE_TOKEN'],'Accept':'text/event-stream'})\n"
    "out=[]\n"
    "try:\n"
    "    for line in urllib.request.urlopen(r,timeout=1.5):\n"
    "        line=line.decode()\n"
    "        if line.startswith('data:'): out.append(json.loads(line[5:]))\n"
    "except (TimeoutError, OSError): pass\n"
    "print(json.dumps(out))\n"
)

results = []
LOG = None


def check(what, ok, detail=""):
    results.append(ok)
    line = f"{'PASS' if ok else 'FAIL'} {what}{(': ' + detail) if detail else ''}"
    print(line, flush=True)
    if LOG is not None:
        LOG.write(line + "\n")
    return ok


def service_get(container, path, script=SERVICE_GET):
    out = subprocess.run(["docker", "exec", container, "python", "-c", script, path],
                         capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(f"service GET {path.split('?')[0]} failed")
    return json.loads(out.stdout)


def docker_logs(container, since):
    out = subprocess.run(["docker", "logs", "--since", since, container], capture_output=True, text=True, timeout=30)
    return out.stdout + out.stderr


async def wait_until(pred, seconds):
    deadline = time.monotonic() + seconds
    while not pred():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


class Run:
    def __init__(self, a, log):
        self.a = a
        self.log = log
        args = types.SimpleNamespace(device=a.device, ws=a.ws, base=a.base, tap=None, receipts=True,
                                     weak_link=0, old_firmware=False, cut_after_ms=0, ws_delay=0, ws_up_delay=0,
                                     level=2)
        self.dev = agent_sim_device.SimDevice(args)
        self.round_no = 0

    def current(self):
        return (self.dev.chats or {}).get("current")

    def ids(self):
        return [c["id"] for c in (self.dev.chats or {}).get("chats") or []]

    async def respond(self, kind, chat_id=None):
        params = {"kind": kind, "via": "tap"}
        if chat_id:
            params["chat_id"] = chat_id
        seq = self.dev.send_event("agent_response", params)
        await wait_until(lambda: seq in self.dev.receipts or seq not in self.dev.awaiting, 20)
        return self.dev.receipts.get(seq, {}).get("result")

    async def voice(self, text):
        """One voice turn; returns (chat id, trace id) of its turn_latency line."""
        self.round_no += 1
        with open(self.a.transcripts, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 1))
        proc = await asyncio.create_subprocess_exec(
            sys.executable, self.a.voice_sim, "--base", self.a.base, "--ws", self.a.ws, "--device", self.a.device,
            "--direct", "--wake", "Jarvis", "--reply", self.a.clip, "--reply-delay", "0.3", "--max-seconds", "30",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        await asyncio.sleep(1)
        logs = docker_logs(self.a.container, since)
        lines = [l for l in logs.splitlines() if "turn_latency" in l]
        self.log.write(f"## round {self.round_no}: {text}\n{out.decode(errors='replace')}\n" + "\n".join(lines) + "\n")
        self.log.flush()
        m = re.search(r"turn_latency trace=(\S+) chat=(\S+)", lines[-1]) if lines else None
        return (m.group(2), m.group(1)) if m else (None, None)

    def landed(self, chat_id, trace, chats):
        """Chats whose events hold an `accepted` event with trace."""
        hits = []
        for cid in chats:
            events = service_get(self.a.container, f"/v1/chats/{quote(cid, safe='')}/events?after=0", EVENTS_GET)
            if any(e.get("type") == "accepted" and e.get("trace_id") == trace for e in events):
                hits.append(cid)
        return hits


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True)
    ap.add_argument("--ws", required=True)
    ap.add_argument("--device", required=True)
    ap.add_argument("--container", required=True, help="the sim xiaozhi server container")
    ap.add_argument("--transcripts", required=True, help="the sim stack's data/sim_transcripts.txt")
    ap.add_argument("--clip", required=True, help="16 kHz mono WAV sent as the user's utterance")
    ap.add_argument("--chats", type=int, default=5)
    ap.add_argument("--switches", type=int, default=10)
    ap.add_argument("--restart", default=None, help="agent-service container to restart at the end")
    ap.add_argument("--restart-gateway", action="store_true", help="then restart --container (the gateway)")
    ap.add_argument("--log", default="agent-chats-e2e.out")
    a = ap.parse_args()
    a.voice_sim = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proactive_sim_device.py")
    with open(a.log, "w", encoding="utf-8") as log:
        agent_sim_device.log = lambda *parts: (log.write(f"{time.time():.3f} " + " ".join(map(str, parts)) + "\n"),
                                               log.flush())
        global LOG
        LOG = log
        run = Run(a, log)
        poller = asyncio.create_task(run.dev.poll_loop(time.time() + 3600))
        try:
            await body(run, a)
        finally:
            poller.cancel()
            await asyncio.gather(poller, return_exceptions=True)
    print(f"{sum(results)} passed, {len(results) - sum(results)} failed (log: {a.log})")
    return 0 if all(results) else 1


async def body(run, a):
    check("initial agent_chats list reaches the device", await wait_until(lambda: run.dev.chats is not None, 20))
    ours = []
    for i in range(a.chats):
        before = set(run.ids())
        t0 = time.monotonic()
        result = await run.respond("new_chat")
        ok = await wait_until(lambda: run.current() and run.current() not in before, LIST_WAIT_S)
        new = run.current()
        check(f"new_chat {i + 1}: receipt accepted and the list's current is the new chat", result == "accepted" and ok,
              f"result={result} current={new} {int((time.monotonic() - t0) * 1000)} ms")
        if not ok:
            return
        ours.append(new)
        chat, trace = await run.voice(f"Chat {i + 1}: let's talk about {TOPICS[i % len(TOPICS)]}.")
        check(f"chat {i + 1}: first turn landed in it", chat == new, f"turn chat={chat}")
    titles = {c["id"]: c.get("title") for c in run.dev.chats["chats"]}
    check("titles from the service's first-prompt title", all(titles.get(c) for c in ours),
          json.dumps({c[-6:]: titles.get(c) for c in ours}, ensure_ascii=False))
    landed_ok = 0
    for k in range(a.switches):
        target = ours[SWITCH_ORDER[k % len(SWITCH_ORDER)] % len(ours)]
        if target == run.current():
            target = ours[(ours.index(target) + 1) % len(ours)]
        t0 = time.monotonic()
        result = await run.respond("switch_chat", target)
        ok = await wait_until(lambda: run.current() == target, LIST_WAIT_S)
        ms = int((time.monotonic() - t0) * 1000)
        check(f"switch {k + 1}: receipt accepted, list current = target", result == "accepted" and ok,
              f"target={target} result={result} {ms} ms")
        chat, trace = await run.voice(f"Follow-up {k + 1}: what were we talking about?")
        hits = run.landed(chat, trace, ours) if trace else []
        good = chat == target and hits == [target]
        landed_ok += good
        check(f"switch {k + 1}: follow-up landed in the selected chat (turn_latency + chat_events)", good,
              f"turn chat={chat} accepted in {hits}")
    check(f"{a.switches} switches, every follow-up in the right chat", landed_ok == a.switches,
          f"{landed_ok}/{a.switches}")
    if a.restart:
        await restart(run, a, ours)
    if a.restart_gateway:
        await restart_gateway(run, a, ours)


async def restart(run, a, ours):
    before = service_get(a.container, f"/v1/chats?device_id={quote(a.device, safe='')}&limit=12")
    seq_before = run.dev.chats_seq
    subprocess.run(["docker", "restart", a.restart], capture_output=True, timeout=120)
    up = False
    for _ in range(60):
        try:
            after = service_get(a.container, f"/v1/chats?device_id={quote(a.device, safe='')}&limit=12")
            up = True
            break
        except (RuntimeError, ValueError, subprocess.TimeoutExpired):
            time.sleep(1)
    check("agent-service back after the restart", up)
    if not up:
        return
    ids = lambda listing: [c["chat_id"] for c in listing["items"]]
    check("chat index unchanged after the restart", ids(after) == ids(before) and
          [c["title"] for c in after["items"]] == [c["title"] for c in before["items"]])
    check("current chat unchanged after the restart", after["current_chat_id"] == before["current_chat_id"],
          f"current={after['current_chat_id']}")
    current = before["current_chat_id"]
    resent = await wait_until(lambda: run.dev.chats_seq > seq_before or run.current() == current, 40)
    check("device list after the restart: same chats, same current",
          resent and run.current() == current and all(c in run.ids() for c in ours),
          f"current={run.current()} seq {seq_before}->{run.dev.chats_seq}")
    chat, trace = await run.voice("After the restart: are you still there?")
    hits = run.landed(chat, trace, ours) if trace else []
    check("follow-up after the restart lands in the current chat", chat == current and hits == [current],
          f"turn chat={chat} accepted in {hits}")


async def restart_gateway(run, a, ours):
    current, seq_before = run.current(), run.dev.chats_seq
    subprocess.run(["docker", "restart", a.container], capture_output=True, timeout=120)
    resent = await wait_until(lambda: run.dev.chats_seq > seq_before, 60)
    check("gateway restart: the list reaches the device again with the same current and chats",
          resent and run.current() == current and all(c in run.ids() for c in ours),
          f"current={run.current()} seq {seq_before}->{run.dev.chats_seq}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
