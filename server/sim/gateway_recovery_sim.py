#!/usr/bin/env python3
"""Gateway recovery sim rounds against server/sim/scratch_agent_service.py: the real agent gateway
and proactive queue (server/overlay) talking HTTP to the scratch service, with simulated device
sessions (playback, played acks, card answers) in place of the board. Rounds: decisions (F04),
speech hand-off (F09), retries and attempts (F10), gateway restart (F11), INTERRUPT (F13), answer
routing and open-card tiers (routing), open cards reconciled against the service's task state
(reconcile, finding 76), spoken results read and a decided card's speech withdrawn (seen, decision 84), and
`--rounds activity`: a turn of fake tools whose working items carry the activity label (decision 68;
start the scratch service with --reply-hold-s 1), and `hold`: an approval card that stays the task's item
through a steer and a later message while its hold is pending (finding 86). Run with the server test venv after starting the
scratch service:

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
import uuid
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
    """A level-2 device session: idle unless speaking() is set; plays a sentence for `audio_s`. Each
    session is a new device boot."""

    boots = 4242

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
        DeviceSession.boots += 1
        self.boot = DeviceSession.boots
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

    async def api(self, method, path, body=None):
        """(status, json) of an agent-service API call."""
        headers = {"Idempotency-Key": str(uuid.uuid4())}
        async with self.http.request(method, self.url + path, json=body, headers=headers) as resp:
            return resp.status, await resp.json()

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

    def receipts(self, conn, card=None):
        return [p for p in conn.websocket.payloads()
                if p.get("kind") == "agent_ack" and card in (None, p.get("card_id"))]

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


async def round_questions(sim, conn):
    print("\n== question cards (decision 67) ==", flush=True)
    asked = await sim.ctl("POST", "/sim/ask", {"device_id": DEVICE, "question": "What would you like me to do next?",
                                               "options": ["Lights", "Music"]})
    await asyncio.sleep(1)
    cards = [e for e in sim.gw.devices[DEVICE].mirror.values() if e.payload.get("state") == "needs_input"]
    check(asked["interaction_id"] is None and not cards,
          f"open-ended question: no interaction ({asked['interaction_id']}), {len(cards)} question card mirrored")
    asked = await sim.ctl("POST", "/sim/ask", {"device_id": DEVICE, "question": "Which light?",
                                               "options": ["Living room", "Bedroom"]})
    entry = await sim.wait(lambda: next((e for e in sim.gw.devices[DEVICE].mirror.values()
                                         if e.payload.get("card_id") == asked["interaction_id"]), None),
                           10, "real choice mirrored")
    item = entry.payload if entry else {}
    check(item.get("state") == "needs_input" and item.get("summary") == "Which light?" and
          [o["label"] for o in item.get("options") or []] == ["Living room", "Bedroom"],
          f"real choice: one card, question as summary, options {[o['label'] for o in item.get('options') or []]}")
    params = {"v": 1, "boot": conn.boot, "seq": conn.next_seq(), "kind": "answer", "chat_id": item.get("chat_id"),
              "task_id": item.get("task_id"), "card_id": asked["interaction_id"], "option_id": "opt1", "via": "tap"}
    sim.gw.on_device_event(conn, wad.RESPONSE_METHOD, params)
    got = await sim.wait(lambda: sim.receipts(conn, asked["interaction_id"]), 20, "receipt of the option tap")
    rows = await sim.ctl("GET", "/sim/rows", params={"interaction_id": asked["interaction_id"]})
    check(got and got[-1]["result"] == "accepted" and rows["interaction"]["state"] == "answered",
          f"option tap: receipt {got and got[-1]['result']}, interaction {rows['interaction']['state']}")


def mirrored_card(sim, card):
    return next((e for e in sim.gw.devices[DEVICE].mirror.values() if e.payload.get("card_id") == card), None)


async def round_routing(sim, conn):
    print("\n== answer routing and open-card tiers ==", flush=True)
    asked = await sim.ctl("POST", "/sim/ask", {"device_id": DEVICE, "question": "Which room?",
                                               "options": ["Living room", "Bedroom"], "origin": "voice"})
    card = asked["interaction_id"]
    entry = await sim.wait(lambda: mirrored_card(sim, card), 10, "voice question mirrored")
    item = entry.payload if entry else {}
    check(item.get("delivery") == "when_idle" and item.get("speaks") is False,
          f"question spoken in the voice reply: card {item.get('delivery')} speaks={item.get('speaks')}")
    _, chat = await sim.api("POST", "/v1/chats", {"device_id": DEVICE, "switch": False})
    status, probe = await sim.api("POST", f"/v1/chats/{chat['chat_id']}/messages",
                                  {"device_id": DEVICE, "text": "Bedroom probe", "origin": {"kind": "api"}})
    rows = await sim.ctl("GET", "/sim/rows", params={"interaction_id": card})
    check(status == 201 and probe.get("chat_id") == chat["chat_id"] and probe.get("routed") == "new_task" and
          rows["interaction"]["state"] == "pending",
          f"API message to another chat in the voice window: {probe.get('routed')} in "
          f"{'its chat' if probe.get('chat_id') == chat['chat_id'] else probe.get('chat_id')}, "
          f"question {rows['interaction']['state']}")
    status, answer = await sim.api("POST", "/v1/chats/current/messages",
                                   {"device_id": DEVICE, "text": "Bedroom", "origin": {"kind": "voice"}})
    rows = await sim.ctl("GET", "/sim/rows", params={"interaction_id": card})
    check(answer.get("routed") == "answer" and rows["interaction"]["state"] == "answered",
          f"voice follow-up: {answer.get('routed')}, question {rows['interaction']['state']}")
    asked = await sim.ctl("POST", "/sim/ask", {"device_id": DEVICE, "question": "Which room?",
                                               "options": ["Living room", "Bedroom"], "origin": "api",
                                               "chat_id": chat["chat_id"]})
    entry = await sim.wait(lambda: mirrored_card(sim, asked["interaction_id"]), 10, "API question mirrored")
    item = entry.payload if entry else {}
    check(item.get("delivery") == "when_idle" and item.get("speaks") is True,
          f"question from an API message: card {item.get('delivery')} speaks={item.get('speaks')}")
    params = {"v": 1, "boot": conn.boot, "seq": conn.next_seq(), "kind": "answer", "chat_id": item.get("chat_id"),
              "task_id": item.get("task_id"), "card_id": asked["interaction_id"], "option_id": "opt2", "via": "tap"}
    sim.gw.on_device_event(conn, wad.RESPONSE_METHOD, params)
    got = await sim.wait(lambda: sim.receipts(conn, asked["interaction_id"]), 20, "receipt of the option tap")
    rows = await sim.ctl("GET", "/sim/rows", params={"interaction_id": asked["interaction_id"]})
    check(got and got[-1]["result"] == "accepted" and rows["interaction"]["state"] == "answered",
          f"option 2 tap: receipt {got and got[-1]['result']}, interaction {rows['interaction']['state']}")


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
    got = await sim.wait(lambda: sim.receipts(conn, card), 20, "receipt after the 503 window")
    check(got and got[-1]["result"] == "stale", f"503 for the whole window: receipt {got and got[-1]['result']} (never accepted)")
    row = (await sim.ctl("GET", "/sim/rows", params={"interaction_id": card}))["interaction"]
    check(row["state"] == "pending", f"service interaction still {row['state']}")
    await sim.clear()
    await sim.fault(r"/answer$", 503, count=1, after=True)
    params = dict(params, seq=conn.next_seq())
    sim.gw.on_device_event(conn, wad.RESPONSE_METHOD, params)
    got = await sim.wait(lambda: len(sim.receipts(conn, card)) == 2 and sim.receipts(conn, card), 20,
                         "receipt of the second tap")
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


async def round_activity(sim, conn):
    print("\n== decision 68 activity label ==", flush=True)
    async with sim.http.post(sim.url + "/v1/chats/current/messages", headers={"Idempotency-Key": str(uuid.uuid4())},
                             json={"device_id": DEVICE, "text": "tools:web_search,ha_get_state,ha_call_service,"
                                                                "tool_search_tool_bm25"}) as resp:
        sent = await resp.json()
    task_id = sent["task_id"]

    def items():
        return [p for p in conn.websocket.payloads() if p.get("kind") == "agent" and p.get("task_id") == task_id]

    await sim.wait(lambda: any(p["state"] != "working" for p in items()), 30, "the activity turn ends")
    labels = []
    for p in items():
        label = (p.get("progress") or {}).get("activity")
        if p["state"] == "working" and label and (not labels or labels[-1] != label):
            labels.append(label)
    print(f"working item labels: {labels}", flush=True)
    want = ["Searching…", "Checking the house…", "Acting…", "Finding a tool…"]
    check([x for x in labels if x in want] == want, f"labels follow the tools in order: {labels}")
    check(all(wad.wire.size(p) <= wad.wire.MAX_ITEM_BYTES for p in items()), "every item within the item budget")
    check(items()[-1]["state"] == "unread" and "progress" not in items()[-1], "the result item has no activity")


def reboot(sim, conn, name):
    """The device reboots: its session closes and a new one opens without the delivery stream."""
    conn.stop_event.set()
    fresh = DeviceSession(name)
    sim.proactive.on_session_open(DEVICE, fresh)
    sim.gw.on_agent_session(DEVICE, fresh, 2)
    fresh.shown_task = asyncio.get_running_loop().create_task(sim.ack_shown(fresh))
    return fresh


def snapshot_of(conn):
    """(task id → state of its last item, agent_sync task ids) once a snapshot ended on conn."""
    payloads = conn.websocket.payloads()
    states = {p["task_id"]: p["state"] for p in payloads if p.get("kind") == "agent"}
    syncs = [p["task_ids"] for p in payloads if p.get("kind") == "agent_sync"]
    return states, (syncs[-1] if syncs else None)


async def round_reconcile(sim, conn):
    print("\n== finding 76 open cards reconciled ==", flush=True)
    asked = await sim.ctl("POST", "/sim/ask", {"device_id": DEVICE, "question": "Which lamp?",
                                               "options": ["Desk", "Floor"]})
    card, task, chat = asked["interaction_id"], asked["task_id"], asked["chat_id"]
    await sim.wait(lambda: mirrored_card(sim, card), 10, "question card mirrored")
    stream = sim.gw._sources[DEVICE]
    stream.cancel()
    await asyncio.gather(stream, return_exceptions=True)
    status, body = await sim.api("POST", f"/v1/chats/{chat}/interrupt",
                                 {"decision_id": str(uuid.uuid4()), "reason": "panel"})
    await asyncio.sleep(1)
    entry = sim.gw.devices[DEVICE].mirror.get(task)
    check(status == 200 and body["task"]["state"] == "cancelled" and entry is not None and
          entry.payload["state"] == "needs_input",
          f"task cancelled in the service ({body.get('task', {}).get('state')}) while the delivery stream is down: "
          f"mirror still {entry and entry.payload['state']}")
    _, snap = await sim.api("GET", f"/v1/devices/{DEVICE}/snapshot")
    states = [c["payload"]["state"] for c in snap["cards"] if c["task_id"] == task]
    check(states == ["cancelled"], f"service snapshot of the task: {states}")
    fresh = reboot(sim, conn, "reboot-1")
    await sim.wait(lambda: snapshot_of(fresh)[1] is not None, 10, "snapshot after the reconcile")
    states, sync = snapshot_of(fresh)
    check(states.get(task) == "cancelled" and task in sync and card not in json.dumps(fresh.websocket.payloads()),
          f"after the reboot: task {states.get(task)}, card {card} not sent")
    chats = [p for p in fresh.websocket.payloads() if p.get("kind") == "agent_chats"]
    chip = next((c["state"] for c in (chats[-1]["chats"] if chats else []) if c["id"] == chat), None)
    check(chip in ("idle", "unread"), f"chat chip after the reconcile: {chip}")

    del sim.gw._sources[DEVICE]
    sim.gw.watch_device(DEVICE)
    ghost = "tsk_sim_ghost"
    payload = {"task_id": ghost, "chat_id": chat, "state": "needs_input", "summary": "Ghost question?", "lang": "en",
               "interaction": {"interaction_id": "int_sim_ghost", "question": {"options": [
                   {"id": "opt1", "label": "Yes"}, {"id": "opt2", "label": "No"}]}}}
    await sim.ctl("POST", "/sim/enqueue", {"device_id": DEVICE, "tier": "WHEN_IDLE", "slot": "int:int_sim_ghost",
                                           "payload": payload, "speak": None, "task_id": ghost, "chat_id": chat})
    await sim.wait(lambda: ghost in sim.gw.devices[DEVICE].mirror, 10, "card of an unknown task mirrored")
    fresh = reboot(sim, fresh, "reboot-2")
    await sim.wait(lambda: snapshot_of(fresh)[1] is not None, 10, "snapshot after the second reconcile")
    states, sync = snapshot_of(fresh)
    check(ghost not in sync and ghost not in sim.gw.devices[DEVICE].mirror,
          f"a card of a task the service does not know is dropped: agent_sync {sync}")

    _, other = await sim.api("POST", "/v1/chats", {"device_id": DEVICE, "switch": False})
    asked = await sim.ctl("POST", "/sim/ask", {"device_id": DEVICE, "question": "Which fan?",
                                               "options": ["Hall", "Attic"], "chat_id": other["chat_id"]})
    await sim.wait(lambda: mirrored_card(sim, asked["interaction_id"]), 10, "second question mirrored")
    await sim.fault(r"^/v1/tasks/", 503, count=100)
    fresh = reboot(sim, fresh, "reboot-3")
    await sim.wait(lambda: snapshot_of(fresh)[1] is not None, 10, "snapshot with the service unreachable")
    states, sync = snapshot_of(fresh)
    check(states.get(asked["task_id"]) == "needs_input" and asked["task_id"] in sync,
          f"service unreachable: the open card stays ({states.get(asked['task_id'])})")
    await sim.clear()
    await sim.api("POST", f"/v1/chats/{asked['chat_id']}/interrupt", {"decision_id": str(uuid.uuid4()), "reason": "panel"})
    return fresh


async def task_rows(sim, task):
    return await sim.ctl("GET", "/sim/rows", params={"task_id": task})


def card_kept(sim, conn, task, card, since):
    """(mirrored state and card, wire states of the task sent after seq `since`)."""
    entry = sim.gw.devices[DEVICE].mirror.get(task)
    later = [p["state"] for p in conn.websocket.payloads()
             if p.get("kind") == "agent" and p["task_id"] == task and p["seq"] > since]
    return (entry and entry.payload.get("state"), entry and entry.payload.get("card_id")), later


async def round_hold(sim, conn):
    print("\n== finding 86 an open approval card survives a steer and a later message ==", flush=True)
    _, chat = await sim.api("POST", "/v1/chats", {"device_id": DEVICE, "switch": False})
    chat_id = chat["chat_id"]
    conn.speaking(True)
    status, first = await sim.api("POST", f"/v1/chats/{chat_id}/messages",
                                  {"device_id": DEVICE, "text": "hold:probe", "origin": {"kind": "api"}})
    task = first.get("task_id")
    await asyncio.sleep(0.5)
    _, steer = await sim.api("POST", f"/v1/chats/{chat_id}/messages",
                             {"device_id": DEVICE, "text": "also tell me the time", "origin": {"kind": "api"}})
    check(status == 201 and steer.get("routed") == "steer" and steer.get("task_id") == task,
          f"second message during the held call: {steer.get('routed')}")
    entry = await sim.wait(lambda: next((e for e in sim.gw.devices[DEVICE].mirror.values()
                                         if e.payload.get("task_id") == task and e.payload.get("state") == "needs_approval"
                                         and e.payload.get("options")), None), 15, "actionable approval card mirrored")
    card = entry.payload["card_id"] if entry else None
    since = entry.payload["seq"] if entry else 0
    speech = await sim.wait(lambda: next((s for s in sim.gw.speeches.values() if s.card_id == card
                                          and s.state == "handed_off"), None), 10, "card speech waiting (device busy)")
    await asyncio.sleep(4)
    rows = await task_rows(sim, task)
    held = [r for r in rows["approvals"] if r["tool"] == "ha_call_service"]
    check(len(rows["interactions"]) == 1 and len(held) == 1 and rows["interactions"][0]["state"] == "pending",
          f"steer joined the hold: {len(rows['interactions'])} card, {len(held)} held call "
          f"({rows['interactions'][0]['state'] if rows['interactions'] else None})")
    (state, kept), later = card_kept(sim, conn, task, card, since)
    check(state == "needs_approval" and kept == card and "working" not in later,
          f"after the steer turn: mirror {state} card {'same' if kept == card else kept}, later items {later}")
    msg = sim.proactive.messages.get(speech.proactive_id) if speech else None
    check(speech is not None and speech.state == "handed_off" and msg is not None and msg.status == "queued",
          f"card speech still queued ({speech and speech.state}, message {msg and msg.status})")
    status, again = await sim.api("POST", f"/v1/chats/{chat_id}/messages",
                                  {"device_id": DEVICE, "text": "tools:ha_get_state", "origin": {"kind": "api"}})
    now = (await task_rows(sim, task))["task"]["state"]
    check(again.get("routed") == "continue" and now == "needs_approval",
          f"later message to the waiting task: {again.get('routed')}, task {now}")
    await asyncio.sleep(7)
    (state, kept), later = card_kept(sim, conn, task, card, since)
    check(state == "needs_approval" and kept == card and "working" not in later,
          f"after the later turn: mirror {state} card {'same' if kept == card else kept}, later items {later}")
    check(speech.state == "handed_off" and msg.status == "queued",
          f"card speech still queued after the later turn ({speech.state}, message {msg.status})")
    params = {"v": 1, "boot": conn.boot, "seq": conn.next_seq(), "kind": "deny", "chat_id": chat_id, "task_id": task,
              "card_id": card, "option_id": "deny", "via": "tap"}
    sim.gw.on_device_event(conn, wad.RESPONSE_METHOD, params)
    got = await sim.wait(lambda: sim.receipts(conn, card), 15, "deny receipt")
    check(got and got[-1]["result"] == "accepted", f"deny tap: receipt {got and got[-1]['result']}")
    final = await sim.wait(lambda: (last_item(conn, task) or {}).get("state") in ("unread", "done", "failed")
                           and last_item(conn, task), 20, "task result after the deny")
    rows = await task_rows(sim, task)
    ran = [e for e in rows["executed"] if (e.get("args") or {}).get("entity_id") == "input_boolean.weixue_session_probe_v2"]
    check(rows["interactions"][0]["state"] == "denied" and not ran and rows["task"]["outcome"] == "denied",
          f"after the deny: interaction {rows['interactions'][0]['state']}, outcome {rows['task']['outcome']}, "
          f"executed {len(ran)}, device {final and final['state']}")
    conn.speaking(False)


def last_item(conn, task):
    return next((p for p in reversed(conn.websocket.payloads()) if p.get("kind") == "agent" and p["task_id"] == task),
                None)


async def round_seen(sim, conn):
    print("\n== decision 84 spoken means read; a decided card's speech is withdrawn ==", flush=True)
    spoken = await sim.enqueue("WHEN_IDLE", "task:seen1", "The dishwasher finished.", task="tsk_seen1")
    r = await sim.wait_rows(spoken, lambda r: r["delivery"]["playback"] == "played", 25, "result played")
    check(json.loads(r["delivery"]["payload_json"]).get("seen") is True, "service: the played result is seen")
    item = await sim.wait(lambda: (last_item(conn, "tsk_seen1") or {}).get("seen") and last_item(conn, "tsk_seen1"),
                          10, "seen item on the device")
    check(item and item["state"] == "unread" and item["delivery"] == "silent" and not item["speaks"],
          f"device got the result again seen:true ({item and item['delivery']}, speaks={item and item['speaks']})")
    await sim.enqueue("SILENT", "task:seen2", None, task="tsk_seen2")
    await sim.wait(lambda: "tsk_seen2" in sim.gw.devices[DEVICE].mirror, 10, "silent result mirrored")
    _, snap = await sim.api("GET", f"/v1/devices/{DEVICE}/snapshot")
    seen = {c["task_id"]: bool(c["payload"].get("seen")) for c in snap["cards"] if c["task_id"] in ("tsk_seen1", "tsk_seen2")}
    check(seen == {"tsk_seen1": True, "tsk_seen2": False}, f"service snapshot seen: {seen}")
    fresh = reboot(sim, conn, "seen-reboot")
    await sim.wait(lambda: snapshot_of(fresh)[1] is not None, 10, "snapshot after the reboot")
    seen = {t: (last_item(fresh, t) or {}).get("seen") for t in ("tsk_seen1", "tsk_seen2")}
    check(seen == {"tsk_seen1": True, "tsk_seen2": False}, f"after a device reboot: {seen}")
    await sim.stop_gateway()
    sim.make_gateway()
    sim.start()
    fresh.stop_event.set()
    fresh = sim.open_session("seen-restart")
    await sim.wait(lambda: snapshot_of(fresh)[1] is not None, 10, "snapshot after a gateway restart")
    seen = {t: (last_item(fresh, t) or {}).get("seen") for t in ("tsk_seen1", "tsk_seen2")}
    check(seen == {"tsk_seen1": True, "tsk_seen2": False}, f"after a gateway restart (service snapshot): {seen}")

    _, own = await sim.api("POST", "/v1/chats", {"device_id": DEVICE, "switch": False})
    asked = await sim.ctl("POST", "/sim/ask", {"device_id": DEVICE, "question": "Which bin?",
                                               "options": ["Green", "Black"], "chat_id": own["chat_id"]})
    card, task, chat = asked["interaction_id"], asked["task_id"], asked["chat_id"]
    await sim.wait(lambda: mirrored_card(sim, card), 10, "question card mirrored")
    fresh.speaking(True)
    text = "Which bin goes out, green or black?"
    payload = {"task_id": task, "chat_id": chat, "state": "needs_input", "summary": "Which bin?", "lang": "en",
               "interaction": {"interaction_id": card, "question": {"options": [
                   {"id": "opt1", "label": "Green"}, {"id": "opt2", "label": "Black"}]}}}
    did = (await sim.ctl("POST", "/sim/enqueue", {"device_id": DEVICE, "tier": "WHEN_IDLE", "slot": f"int:{card}",
                                                  "payload": payload, "speak": text, "task_id": task,
                                                  "chat_id": chat}))["delivery_id"]
    speech = await sim.wait(lambda: next((s for s in sim.gw.speeches.values()
                                          if s.delivery_id == did and s.state == "handed_off"), None),
                            10, "card speech handed off while the device is busy")
    msg = sim.proactive.messages.get(speech.proactive_id) if speech else None
    check(msg is not None and msg.status == "queued", f"card speech waits in the proactive queue ({msg and msg.status})")
    sim.gw.on_device_event(fresh, wad.RESPONSE_METHOD, {"v": 1, "boot": fresh.boot, "seq": fresh.next_seq(),
                                                        "kind": "answer", "chat_id": chat, "task_id": task,
                                                        "card_id": card, "option_id": "opt1", "via": "tap"})
    got = await sim.wait(lambda: sim.receipts(fresh, card), 15, "answer receipt")
    check(got and got[-1]["result"] == "accepted", f"answer receipt {got and got[-1]['result']}")
    check(msg is not None and msg.status == "expired" and speech.state == "dropped",
          f"card speech withdrawn after the answer (message {msg and msg.status}, speech {speech and speech.state})")
    r = await sim.wait_rows(did, lambda r: r["delivery"]["state"] == "dropped", 10, "card speech acked dropped")
    fresh.speaking(False)
    await asyncio.sleep(3)
    check(text not in sim.played_texts, "the decided card's speech never played")
    return fresh


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:18810")
    ap.add_argument("--state", required=True)
    ap.add_argument("--rounds", default="questions,decisions,speech,attempts,restart,interrupt,routing,reconcile,seen,hold",
                    help="comma list of questions, decisions, speech, attempts, restart, interrupt, routing, "
                         "reconcile, seen, hold, activity")
    a = ap.parse_args()
    rounds = a.rounds.split(",")
    token = os.environ["SIM_TOKEN"]
    os.chdir(a.state)
    sim = Sim(a.url, token, a.state)
    sim.http = aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"})
    sim.start()
    conn = sim.open_session("s1")
    await sim.wait(lambda: sim.gw.devices[DEVICE].restored, 10, "first restore")
    if "questions" in rounds:
        await round_questions(sim, conn)
    if "decisions" in rounds:
        await round_decisions(sim, conn)
    if "speech" in rounds:
        await round_speech(sim, conn)
    if "attempts" in rounds:
        await round_attempts(sim, conn)
    if "restart" in rounds:
        conn = await round_restart(sim, conn)
    if "interrupt" in rounds:
        await round_interrupt(sim, conn)
    if "activity" in rounds:
        await round_activity(sim, conn)
    if "routing" in rounds:
        await round_routing(sim, conn)
    if "reconcile" in rounds:
        conn = await round_reconcile(sim, conn)
    if "seen" in rounds:
        conn = await round_seen(sim, conn)
    if "hold" in rounds:
        await round_hold(sim, conn)
    await sim.stop_gateway()
    await sim.http.close()
    print(f"\n{'ALL PASS' if not FAILURES else 'FAILED: ' + '; '.join(FAILURES)}", flush=True)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
