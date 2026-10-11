#!/usr/bin/env python3
"""Gateway recovery sim rounds against server/sim/scratch_agent_service.py: the real agent gateway
and proactive queue (server/overlay) talking HTTP to the scratch service, with simulated device
sessions (playback, played acks, card answers) in place of the board. Rounds: decisions (F04),
speech hand-off (F09), retries and attempts (F10), gateway restart (F11), INTERRUPT (F13). Run
with the server test venv after starting the scratch service:

    SIM_TOKEN=<same token> python server/sim/gateway_recovery_sim.py --url http://127.0.0.1:18810 --state <tmp dir>

Exits 1 when a check fails; every check prints PASS or FAIL."""
import argparse
import asyncio
import json
import os
import sys
import threading
import time
import types
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
VENDOR = Path(os.environ.get("WEIXUE_VENDOR", Path.home() / "Projects" / "weixue-vendor")) / \
    "xiaozhi-esp32-server" / "main" / "xiaozhi-server"
sys.path[:0] = [str(SERVER / "overlay"), str(VENDOR)]
sys.dont_write_bytecode = True


class Logger:
    def bind(self, **_):
        return self

    def __getattr__(self, level):
        def log(message, *_a, **_k):
            print(f"{time.strftime('%H:%M:%S')} {level[:4]} {message}", flush=True)
        return log


LOG = Logger()
for name, attrs in {"config.logger": {"setup_logging": lambda *a, **k: LOG},
                    "core.handle.receiveAudioHandle": {"startToChat": None, "handleAudioMessage": None},
                    "core.handle.reportHandle": {"enqueue_asr_report": None, "enqueue_tts_report": None,
                                                 "enqueue_tool_report": None}}.items():
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module

import aiohttp  # noqa: E402

import weixue_agent_delivery as wad  # noqa: E402
import weixue_proactive as wp  # noqa: E402

DEVICE = "7c:0c:5f:42:4b:fc"
FAILURES = []


def check(ok, what):
    print(f"{'PASS' if ok else 'FAIL'} {what}", flush=True)
    if not ok:
        FAILURES.append(what)


class Socket:
    def __init__(self):
        self.sent = []
        self.request = types.SimpleNamespace(headers={"device-id": DEVICE})

    async def send(self, data):
        self.sent.append(data)

    def payloads(self):
        return [json.loads(s)["payload"] for s in self.sent if isinstance(s, str) and s.startswith('{"type":"custom"')]


class Dialogue:
    def __init__(self):
        self.dialogue = []

    def put(self, m):
        self.dialogue.append(m)


class DeviceSession:
    """A level-2 device session: idle unless speaking() is set; plays a sentence for `audio_s`."""

    def __init__(self, name):
        self.session_id = name
        self.headers = {"device-id": DEVICE}
        self.websocket = Socket()
        self.stop_event = threading.Event()
        self.tts = object()
        self.client_is_speaking = False
        self.client_have_voice = False
        self.client_abort = False
        self.features = {"mcp": True, "agent": 2}
        old = time.time() * 1000 - 10_000
        self.first_activity_time = old
        self.last_activity_time = old
        self.dialogue = Dialogue()
        self.sentence_id = None
        self.closed = False
        self.boot = 4242
        self.seq = 0

    def speaking(self, on):
        self.client_is_speaking = on
        if on:
            self.last_activity_time = time.time() * 1000

    async def close(self):
        self.closed = True
        self.stop_event.set()

    def next_seq(self):
        self.seq += 1
        return self.seq


class Sim:
    def __init__(self, url, token, state_dir):
        self.url, self.token, self.state_dir = url, token, Path(state_dir)
        self.audio_s = {}
        self.no_audio = set()
        self.played_texts = []
        self.http = None
        self.make_gateway()

    def make_gateway(self):
        proactive = wp.Proactive(self.token, auth=wp.DeviceAuth(True, [DEVICE]), speaker=self.speaker,
                                 interrupter=self.interrupter, state=wp.StateFile(str(self.state_dir / "proactive.json")))
        sink = wad.ServiceSink(LOG, self.url, self.token)
        gw = wad.AgentGateway(proactive, sink=sink, state=wp.StateFile(str(self.state_dir / "agent.json")))
        gw.service_url, gw.service_token = self.url, self.token
        proactive.agent = gw
        self.proactive, self.gw = proactive, gw

    def start(self):
        self.proactive.start()
        self.gw.start()

    async def stop_gateway(self):
        tasks = [self.proactive._dispatcher, self.gw._runner, *self.gw._sources.values(),
                 *self.gw.sink._ack_workers.values()]
        for t in tasks:
            if t is not None:
                t.cancel()
        await asyncio.gather(*[t for t in tasks if t is not None], return_exceptions=True)
        if self.gw.sink._session is not None:
            await self.gw.sink._session.close()

    async def speaker(self, conn, item, close_after):
        text = item.text
        sid = f"s-{item.id[:8]}"
        if item.agent is not None:
            item.agent["sentence_id"] = sid
            conn._weixue_ack_sentence = sid
        conn.sentence_id = sid
        conn.client_abort = False
        if text in self.no_audio:
            return "no_audio"
        conn.speaking(True)
        LOG.info(f"device plays '{text}' sentence={sid}")
        end = time.monotonic() + self.audio_s.get(text, 0.4)
        while time.monotonic() < end:
            if conn.client_abort or conn.sentence_id != sid:
                LOG.info(f"device playback of '{text}' stopped by the server")
                conn.speaking(False)
                return "interrupted"
            await asyncio.sleep(0.05)
        conn.speaking(False)
        self.played_texts.append(text)
        self.gw.on_tts(sid, "stop")
        self.gw.on_device_event(conn, wad.PLAYED_METHOD, {"v": 1, "boot": conn.boot, "seq": conn.next_seq(),
                                                          "sentence_id": sid, "complete": True})
        return "spoken"

    async def interrupter(self, conn):
        LOG.info(f"server abort on session {conn.session_id}")
        conn.client_abort = True
        conn.client_is_speaking = False

    # agent-service control

    async def ctl(self, method, path, body=None, params=None):
        async with self.http.request(method, self.url + path, json=body, params=params) as resp:
            return await resp.json()

    async def rows(self, delivery_id):
        return await self.ctl("GET", "/sim/rows", params={"delivery_id": delivery_id})

    async def enqueue(self, tier, slot, speak, task="tsk_sim", chat="cht_sim", state="done"):
        payload = {"task_id": task, "chat_id": chat, "state": state, "summary": speak or "card", "lang": "en",
                   "unread": True}
        r = await self.ctl("POST", "/sim/enqueue", {"device_id": DEVICE, "tier": tier, "slot": slot, "payload": payload,
                                                    "speak": speak, "task_id": task, "chat_id": chat, "ttl_s": 600})
        return r["delivery_id"]

    async def fault(self, path, status=503, count=1, after=False):
        await self.ctl("POST", "/sim/faults", {"path": path, "status": status, "count": count, "after": after})

    async def clear(self):
        await self.ctl("POST", "/sim/faults/clear", {})

    async def wait(self, pred, seconds, what):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            value = pred() if not asyncio.iscoroutinefunction(pred) else await pred()
            if value:
                return value
            await asyncio.sleep(0.1)
        check(False, f"timed out: {what}")
        return None

    async def wait_rows(self, delivery_id, pred, seconds, what):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            r = await self.rows(delivery_id)
            if pred(r):
                return r
            await asyncio.sleep(0.2)
        r = await self.rows(delivery_id)
        check(False, f"timed out: {what} (state={r['delivery'] and r['delivery']['state']}, "
                     f"acks={[a['outcome'] for a in r['acks']]})")
        return r

    def receipts(self, conn):
        return [p for p in conn.websocket.payloads() if p.get("kind") == "agent_ack"]

    def open_session(self, name):
        conn = DeviceSession(name)
        self.proactive.on_session_open(DEVICE, conn)
        self.gw.on_agent_session(DEVICE, conn, 2)
        self.gw.watch_device(DEVICE)
        conn.shown_task = asyncio.get_running_loop().create_task(self.ack_shown(conn))
        return conn

    async def ack_shown(self, conn):
        """The device's `shown` acks: what arrived on the session, every 0.5 s."""
        acked = 0
        while not conn.stop_event.is_set():
            await asyncio.sleep(0.5)
            seqs = [p.get("seq", 0) for p in conn.websocket.payloads() if p.get("kind") in ("agent", "agent_chats", "agent_sync")]
            upto = max(seqs, default=0)
            if upto > acked:
                acked = upto
                self.gw.on_device_event(conn, wad.RESPONSE_METHOD, {"v": 1, "boot": conn.boot, "seq": conn.next_seq(),
                                                                    "kind": "shown", "ack_upto": upto, "via": "tap"})


async def round_decisions(sim, conn):
    print("\n== F04 decision receipts ==", flush=True)
    asked = await sim.ctl("POST", "/sim/ask", {"device_id": DEVICE, "question": "Which day are the bins?",
                                               "options": ["Monday", "Thursday"]})
    card = asked["interaction_id"]
    entry = await sim.wait(lambda: next((e for e in sim.gw.devices[DEVICE].mirror.values()
                                         if e.payload.get("card_id") == card), None), 10, "question card mirrored")
    task, chat = entry.payload["task_id"], entry.payload["chat_id"]
    await sim.fault(r"/answer$", 503, count=100)
    params = {"v": 1, "boot": conn.boot, "seq": conn.next_seq(), "kind": "answer", "chat_id": chat, "task_id": task,
              "card_id": card, "option_id": "opt2", "via": "tap"}
    first = sim.gw.on_device_event(conn, wad.RESPONSE_METHOD, params)
    check(first is None, "no receipt while the service is answering 503")
    got = await sim.wait(lambda: sim.receipts(conn), 20, "receipt after the 503 window")
    check(got and got[-1]["result"] == "stale", f"503 for the whole window: receipt {got and got[-1]['result']} (never accepted)")
    row = (await sim.ctl("GET", "/sim/rows", params={"interaction_id": card}))["interaction"]
    check(row["state"] == "pending", f"service interaction still {row['state']}")
    await sim.clear()
    await sim.fault(r"/answer$", 503, count=1, after=True)
    params = dict(params, seq=conn.next_seq())
    sim.gw.on_device_event(conn, wad.RESPONSE_METHOD, params)
    got = await sim.wait(lambda: len(sim.receipts(conn)) == 2 and sim.receipts(conn), 20, "receipt of the second tap")
    check(got and got[-1]["result"] == "accepted",
          f"answer handled but its reply lost, retried with the same decision id: receipt {got and got[-1]['result']}")
    rows = await sim.ctl("GET", "/sim/rows", params={"interaction_id": card})
    check(rows["interaction"]["state"] == "answered" and len(rows["decisions"]) == 1,
          f"service: interaction {rows['interaction']['state']}, {len(rows['decisions'])} decision row")
    third = sim.gw.on_device_event(conn, wad.RESPONSE_METHOD, dict(params, seq=conn.next_seq()))
    await asyncio.sleep(1)
    rows = await sim.ctl("GET", "/sim/rows", params={"interaction_id": card})
    check(third and third["result"] in ("duplicate", "stale") and len(rows["decisions"]) == 1,
          f"a third tap is not forwarded again: receipt {third and third['result']}, {len(rows['decisions'])} decision row")


async def round_speech(sim, conn):
    print("\n== F09 speech hand-off ==", flush=True)
    await sim.fault(r"/ack$", 503, count=2)
    d1 = await sim.enqueue("WHEN_IDLE", "task:sim1", "Your laundry is done.", task="tsk_sim1")
    r = await sim.wait_rows(d1, lambda r: r["delivery"]["playback"] == "played", 25, "d1 played")
    outcomes = [a["outcome"] for a in r["acks"] if a["outcome"] != "shown"]
    check(outcomes == ["accepted", "spoken", "played"], f"d1 speech acks after two 503s on accepted: {outcomes}")
    msgs = [m for m in sim.proactive.messages.values() if (m.key or "").startswith(d1)]
    check(len(msgs) == 1, f"d1 proactive messages: {len(msgs)}")

    await sim.fault(r"/ack$", 503, count=1000)
    d2 = await sim.enqueue("WHEN_IDLE", "task:sim2", "The oven is preheated.", task="tsk_sim2")
    await sim.wait(lambda: any(s.delivery_id == d2 and s.state == "unaccepted" for s in sim.gw.speeches.values()), 20,
                   "d2 unaccepted")
    check(not [m for m in sim.proactive.messages.values() if (m.key or "").startswith(d2)],
          "d2 not queued while the service cannot record the hand-off")
    await sim.clear()
    r = await sim.wait_rows(d2, lambda r: r["delivery"]["playback"] == "played", 40, "d2 offered again and played")
    msgs = [m for m in sim.proactive.messages.values() if (m.key or "").startswith(d2)]
    check(len(msgs) == 1 and sim.played_texts.count("The oven is preheated.") == 1,
          f"d2 played once after the service's lease expired: messages={len(msgs)}, acks={[a['outcome'] for a in r['acks']]}")


async def round_attempts(sim, conn):
    print("\n== F10 one retry owner ==", flush=True)
    sim.no_audio.add("The door is unlocked.")
    d3 = await sim.enqueue("WHEN_IDLE", "task:sim3", "The door is unlocked.", task="tsk_sim3")
    r = await sim.wait_rows(d3, lambda r: any(a["outcome"] == "deferred" for a in r["acks"]), 30, "d3 deferred")
    sim.no_audio.discard("The door is unlocked.")
    r = await sim.wait_rows(d3, lambda r: r["delivery"]["playback"] == "played", 30, "d3 attempt 2 played")
    keys = sorted(m.key for m in sim.proactive.messages.values() if (m.key or "").startswith(d3))
    check(keys == [f"{d3}:1", f"{d3}:2"] and r["delivery"]["attempt"] == 2,
          f"d3: attempt {r['delivery']['attempt']} offered by the service, proactive keys {keys}")
    speech = [a["outcome"] for a in r["acks"] if a["outcome"] != "shown"]
    check(speech == ["accepted", "deferred", "accepted", "spoken", "played"], f"d3 speech acks: {speech}")


async def round_restart(sim, conn):
    print("\n== F11 gateway restart ==", flush=True)
    before = {t: e.seq for t, e in sim.gw.devices[DEVICE].mirror.items()}
    LOG.info(f"mirror before the restart: {sorted(before)}")
    await sim.stop_gateway()
    sim.make_gateway()
    sim.start()
    conn.stop_event.set()
    fresh = DeviceSession("after-restart")
    sim.proactive.on_session_open(DEVICE, fresh)
    sim.gw.on_agent_session(DEVICE, fresh, 2)
    await asyncio.sleep(0.2)
    check(fresh.websocket.payloads() == [], "no snapshot or agent_sync before the service snapshot is restored")
    sim.gw.watch_device(DEVICE)
    await sim.wait(lambda: any(p.get("kind") == "agent_sync" for p in fresh.websocket.payloads()), 10, "snapshot sent")
    payloads = fresh.websocket.payloads()
    sync = next(p for p in payloads if p["kind"] == "agent_sync")
    check(set(before) <= set(sync["task_ids"]), f"agent_sync lists every task the device held: {sync['task_ids']}")
    restored = {p["task_id"]: p["seq"] for p in payloads if p["kind"] == "agent"}
    same = [t for t in before if restored.get(t) == before[t]]
    check(len(same) == len(before), f"restored cards keep their wire seqs: {len(same)}/{len(before)}")
    check(not any(p.get("speaks") for p in payloads if p["kind"] == "agent"), "restored cards do not speak")
    fresh.shown_task = asyncio.get_running_loop().create_task(sim.ack_shown(fresh))
    return fresh


async def round_interrupt(sim, conn):
    print("\n== F13 INTERRUPT ==", flush=True)
    conn.speaking(True)
    d5 = await sim.enqueue("INTERRUPT", "task:sim5", "Water detected under the sink.", task="tsk_sim5")
    r = await sim.wait_rows(d5, lambda r: r["delivery"]["playback"] == "played", 20, "d5 played during a voice turn")
    check(r["delivery"]["playback"] == "played", "INTERRUPT item played while the session was speaking a voice turn")

    sim.audio_s["Here is the long weather report."] = 6
    d6 = await sim.enqueue("WHEN_IDLE", "task:sim6", "Here is the long weather report.", task="tsk_sim6")
    await sim.wait(lambda: conn.client_is_speaking, 15, "d6 playing")
    d7 = await sim.enqueue("INTERRUPT", "task:sim7", "Smoke alarm in the kitchen.", task="tsk_sim7")
    r7 = await sim.wait_rows(d7, lambda r: r["delivery"]["playback"] == "played", 20, "d7 played")
    r6 = await sim.rows(d6)
    check([a["outcome"] for a in r6["acks"]][-1] == "cut_off" and r6["acks"][-1]["reason"] == "superseded",
          f"d6 cut off by d7: {[a['outcome'] for a in r6['acks']]} reason={r6['acks'][-1]['reason']}")
    check(r6["delivery"]["playback"] == "cut_off" and r7["delivery"]["playback"] == "played",
          f"service: d6 {r6['delivery']['playback']}, d7 {r7['delivery']['playback']}")
    late = sim.gw.on_device_event(conn, wad.PLAYED_METHOD, {"v": 1, "boot": conn.boot, "seq": conn.next_seq(),
                                                            "sentence_id": r6["acks"][-1]["sentence_id"], "complete": True})
    await asyncio.sleep(1)
    r6b = await sim.rows(d6)
    check(late is None and len(r6b["acks"]) == len(r6["acks"]), "a late played ack of the cut sentence is not posted")


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:18810")
    ap.add_argument("--state", required=True)
    a = ap.parse_args()
    token = os.environ["SIM_TOKEN"]
    os.chdir(a.state)
    sim = Sim(a.url, token, a.state)
    sim.http = aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"})
    sim.start()
    conn = sim.open_session("s1")
    await sim.wait(lambda: sim.gw.devices[DEVICE].restored, 10, "first restore")
    await round_decisions(sim, conn)
    await round_speech(sim, conn)
    await round_attempts(sim, conn)
    conn = await round_restart(sim, conn)
    await round_interrupt(sim, conn)
    await sim.stop_gateway()
    await sim.http.close()
    print(f"\n{'ALL PASS' if not FAILURES else 'FAILED: ' + '; '.join(FAILURES)}", flush=True)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
