#!/usr/bin/env python3
"""Sends scripted `agent` messages through the gateway's fixture route and prints the acks and
device responses per item (docs/agent-protocol.md, Fixture).

  WEIXUE_PROACTIVE_TOKEN=... agent_msg_fixture.py --base http://<host>:8003 --device <mac> <scenario>

Scenarios: states (every state, card-only, all tiers), approval, clarify, unread (ack_seen),
burst (duplicate, stale and oversize items), seed + drop (snapshot after a board reset),
busy (WHEN_IDLE speech queued while a session is open), spoken (INTERRUPT and WHEN_IDLE speech),
view (the gateway's deliveries view), ui (the device UI tour: three chats with titles, progress,
a countdown approval, a 3-option question, an irreversible approval, unread, failed and stopped
results, --gap seconds apart; card-only), eight (8 full-size tasks at once, for the paged poll
and the snapshot after a board reset), closing (an announcement and an approval with a loopback
approve, so the approve meets the session the server closes after the announcement), chats (a raw
v1.2 `agent_chats` list of five chats of the ui tour's prefix, current `home`; a v1.1 device drops
it). Output is JSON lines; --log appends them to a file."""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

TIERS = ("when_idle", "silent", "interrupt")


class Fixture:
    def __init__(self, base, device, token, log=None, prefix=None):
        self.base = base.rstrip("/")
        self.device = device.lower()
        self.token = token
        self.log = open(log, "a") if log else None
        self.prefix = prefix or time.strftime("%H%M%S")

    def emit(self, event, **data):
        line = json.dumps({"t": round(time.time(), 3), "event": event, **data}, ensure_ascii=False)
        print(line, flush=True)
        if self.log:
            self.log.write(line + "\n")
            self.log.flush()

    def request(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def send(self, items, drop=None):
        body = {"device": self.device, "items": items}
        if drop:
            body["drop"] = drop
        status, out = self.request("POST", "/weixue/agent/messages", body)
        self.emit("sent", status=status, result=out)
        if status != 202:
            raise SystemExit(f"fixture route answered {status}: {out}")
        return out

    def announce(self, message, mode="announce", urgent=False):
        status, out = self.request("POST", "/weixue/proactive/messages",
                                   {"mode": mode, "message": message, "device": self.device, "urgent": urgent},
                                   {"Idempotency-Key": f"agent-fixture-{self.prefix}-{time.time()}"})
        self.emit("proactive", status=status, result=out)
        return out

    def view(self):
        status, out = self.request("GET", "/weixue/agent/deliveries")
        if status != 200:
            raise SystemExit(f"deliveries view answered {status}")
        return out

    def report(self, records, wait, responses=0, final=False):
        """Waits up to wait seconds (until every item is shown, plus `responses` device responses
        for these tasks, plus a final speech outcome when final), then prints the acks, responses
        and speech states of records."""
        ids = {r["delivery_id"] for r in records if r.get("delivery_id")}
        tasks = {r.get("task_id") for r in records}
        speech = {r.get("speech_id") for r in records if r.get("speech_id")}
        deadline = time.time() + wait
        while True:
            view = self.view()
            acks = [a for a in view["acks"] if a["delivery_id"] in ids]
            shown = {a["delivery_id"] for a in acks if a["outcome"] == "shown"}
            answered = [x for x in view["responses"] if x.get("task_id") in tasks]
            ended = all(s["state"] in ("played", "cut_off", "lost", "dropped")
                        for s in view["speeches"] if s["speech_id"] in speech)
            if time.time() >= deadline or (ids <= shown and len(answered) >= responses and (ended or not final)):
                break
            time.sleep(1)
        for r in records:
            self.emit("item", seq=r.get("seq"), task_id=r.get("task_id"), delivery_id=r.get("delivery_id"),
                      acks=[{k: a[k] for k in a if k not in ("delivery_id", "device")}
                            for a in acks if a["delivery_id"] == r.get("delivery_id")])
        self.emit("responses", responses=[x for x in view["responses"] if x.get("task_id") in tasks])
        if speech:
            self.emit("speeches", speeches=[s for s in view["speeches"] if s["speech_id"] in speech])
        dev = view["devices"].get(self.device, {})
        self.emit("device", acked_upto=dev.get("acked_upto"), agent_sessions=dev.get("agent_sessions"),
                  snapshot_due=dev.get("snapshot_due"), mirror=dev.get("mirror"), unacked=dev.get("unacked"))
        return view

    def task(self, name):
        return f"t_{self.prefix}_{name}"


def item(fx, task, state, tier, summary, chat=None, **extra):
    fields = {"chat_id": f"c_{fx.prefix}{'_' + chat if chat else ''}", "task_id": fx.task(task), "state": state,
              "summary": summary, "delivery": tier, "speaks": False}
    fields.update(extra)
    return fields


def states(fx, args):
    plan = [
        ("s1", "working", "Checking the living room", {"progress": {"step": 1, "total": 3}}),
        ("s1", "interrupted", "Interrupted by a new message", {}),
        ("s1", "needs_input", "Which lights?", {"card_id": f"i_{fx.prefix}_1",
                                               "options": [{"id": "all", "label": "All"}, {"id": "lamp", "label": "Lamp"}]}),
        ("s1", "unread", "Living room lights are on", {"seen": False}),
        ("s2", "needs_approval", "Turn on the desk plug", {"card_id": f"i_{fx.prefix}_2", "risk": "reversible",
                                                           "timeout_s": 300,
                                                           "options": [{"id": "approve", "label": "Approve"},
                                                                       {"id": "deny", "label": "Deny"}]}),
        ("s2", "done", "Desk plug is on", {"seen": True}),
        ("s3", "failed", "Could not reach Home Assistant", {"seen": False}),
        ("s4", "cancelled", "Cancelled", {"seen": False}),
        ("s5", "stopped", "Stopped", {"seen": False}),
    ]
    items = [item(fx, t, st, TIERS[i % 3], summary, **extra) for i, (t, st, summary, extra) in enumerate(plan)]
    records = []
    for it in items:
        records += fx.send([it])["items"]
        time.sleep(args.gap)
    fx.report(records, args.wait)


def approval(fx, args):
    records = fx.send([item(fx, "ap", "needs_approval", "when_idle", "Turn on the hall light", card_id=f"i_{fx.prefix}_ap",
                            risk="reversible", timeout_s=300,
                            options=[{"id": "approve", "label": "Approve"}, {"id": "deny", "label": "Deny"}],
                            debug_reply={"kind": "approve", "option_id": "approve"})])["items"]
    fx.report(records, args.wait, responses=1)


def clarify(fx, args):
    records = fx.send([item(fx, "cl", "needs_input", "when_idle", "Which room?", card_id=f"i_{fx.prefix}_cl",
                            options=[{"id": "kitchen", "label": "Kitchen"}, {"id": "hall", "label": "Hall"}],
                            debug_reply={"kind": "answer", "option_id": "hall"})])["items"]
    fx.report(records, args.wait, responses=1)


def unread(fx, args):
    records = fx.send([item(fx, "ur", "unread", "silent", "The washer finished", seen=False,
                            debug_reply={"kind": "ack_seen"})])["items"]
    fx.report(records, args.wait, responses=1)


def burst(fx, args):
    base = int(time.time() * 1000) + 5000
    a = item(fx, "b1", "working", "silent", "Burst item")
    newer = item(fx, "b1", "needs_input", "silent", "Burst newer", card_id=f"i_{fx.prefix}_b",
                 options=[{"id": "x", "label": "X"}])
    oversize = {"kind": "agent", "v": 1, "seq": base + 10, "chat_id": f"c_{fx.prefix}", "task_id": fx.task("big"),
                "state": "working", "summary": "Oversize", "pad": "x" * 2100}
    items = [
        dict(a, seq=base),
        dict(a, seq=base),
        dict(newer, seq=base + 2),
        dict(a, seq=base + 1),
        {"raw": oversize},
        item(fx, "b2", "working", "silent", "After the burst"),
    ]
    records = fx.send(items)["items"]
    fx.emit("expect", duplicate=base, stale=base + 1, oversize_bytes=len(json.dumps(oversize)))
    fx.report(records, args.wait)


def seed(fx, args):
    records = fx.send([item(fx, "keep", "unread", "silent", "Kept after the reset", seen=False),
                       item(fx, "gone", "working", "silent", "Dropped before the reset")])["items"]
    fx.report(records, args.wait)


def drop(fx, args):
    status, out = fx.request("POST", "/weixue/agent/messages", {"device": fx.device, "items": [],
                                                                "drop": [fx.task(t) for t in args.tasks]})
    fx.emit("dropped", status=status, result=out)


def busy(fx, args):
    fx.announce("Do you want me to keep the hall light on?", mode="conversation")
    time.sleep(args.busy_delay)
    records = fx.send([item(fx, "busy", "unread", "when_idle", "Hall light routine saved", seen=False, speaks=True,
                            speech="I saved the hall light routine.")])["items"]
    fx.report(records, args.wait, final=True)


def spoken(fx, args):
    records = fx.send([item(fx, "sp1", "unread", "when_idle", "Washer finished", seen=False, speaks=True,
                            speech="The washer has finished."),
                       item(fx, "sp2", "failed", "interrupt", "Leak sensor offline", seen=False, speaks=True,
                            speech="The leak sensor is offline.")])["items"]
    fx.report(records, args.wait, final=True)


def ui(fx, args):
    """Device UI tour: every face state, both cards, the management rows and three chats."""
    approve = [{"id": "approve", "label": "Approve"}, {"id": "deny", "label": "Deny"}]
    plan = [
        item(fx, "sun", "working", "silent", "Making the sunset automation", chat="home", title="Sunset lights",
             progress={"step": 1, "total": 5}),
        item(fx, "sun", "working", "silent", "Editing automations.yaml", chat="home", title="Sunset lights",
             progress={"step": 2, "total": 5}),
        item(fx, "hw", "working", "silent", "Checking the homework list", chat="kids", title="Kids' homework"),
        item(fx, "lr", "needs_approval", "when_idle", "Turn on living room lights", chat="home",
             title="Living room lights", card_id=f"i_{fx.prefix}_lr", risk="reversible", timeout_s=300,
             options=approve),
        item(fx, "tidy", "needs_input", "when_idle", "Which room should I start with?", chat="home",
             title="Evening tidy-up", card_id=f"i_{fx.prefix}_tidy",
             options=[{"id": "kitchen", "label": "Kitchen"}, {"id": "bedroom", "label": "Bedroom"},
                      {"id": "hall", "label": "Hallway"}]),
        item(fx, "door", "needs_approval", "silent", "Unlock the front door", chat="home", title="Front door",
             card_id=f"i_{fx.prefix}_door", risk="irreversible", timeout_s=120, options=approve),
        item(fx, "hw", "unread", "when_idle", "Homework list is ready", chat="kids", title="Kids' homework",
             seen=False),
        item(fx, "wr", "unread", "silent", "Weekly report is ready", chat="report", title="Weekly report", seen=False),
        item(fx, "meter", "failed", "when_idle", "Meter API timed out", chat="report", title="Energy report",
             seen=False),
        item(fx, "fan", "stopped", "silent", "Stopped", chat="home", title="Fan schedule", seen=False),
    ]
    records = []
    for it in plan:
        records += fx.send([it])["items"]
        time.sleep(args.gap)
    fx.report(records, args.wait)


def eight(fx, args):
    approve = [{"id": "approve", "label": "Approve"}, {"id": "deny", "label": "Deny"}]
    items = []
    for i in range(8):
        extra = {"title": f"Eight task {i} title text"[:24]}
        if i % 2 == 0:
            extra.update(card_id=f"i_{fx.prefix}_8_{i}", risk="reversible", timeout_s=600, options=approve)
        state = "needs_approval" if i % 2 == 0 else "working"
        items.append(item(fx, f"e{i}", state, "silent",
                          f"Full-size summary for task number {i} of eight, sixty chars"[:60], chat=f"e{i % 3}", **extra))
    records = fx.send(items)["items"]
    fx.report(records, args.wait)


def closing(fx, args):
    fx.announce("Checking the closing window of this session.")
    records = fx.send([item(fx, "cw", "needs_approval", "when_idle", "Turn on the porch light", card_id=f"i_{fx.prefix}_cw",
                            risk="reversible", timeout_s=300,
                            options=[{"id": "approve", "label": "Approve"}, {"id": "deny", "label": "Deny"}],
                            debug_reply={"kind": "approve", "option_id": "approve"})])["items"]
    fx.report(records, args.wait, responses=1)


def chats(fx, _args):
    """Raw agent_chats for a v1.2 device: five chats, the ui tour's chat ids with the same --prefix."""
    names = [("home", "Home lights", "needs_you", 40), ("kids", "Kids' homework", "unread", 900),
             ("report", "Weekly report", "failed", 3 * 3600), ("trip", "周末去海边", "working", 86400),
             ("new", None, "idle", 2 * 86400)]
    entries = []
    for name, title, state, age in names:
        entry = {"id": f"c_{fx.prefix}_{name}", "state": state, "age_s": age}
        if title:
            entry["title"] = title
        entries.append(entry)
    payload = {"kind": "agent_chats", "v": 1, "seq": int(time.time() * 1000), "ts": int(time.time() * 1000),
               "current": f"c_{fx.prefix}_home", "chats": entries}
    fx.emit("expect", seq=payload["seq"], current=payload["current"], chats=len(entries),
            bytes=len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()))
    fx.send([{"raw": payload}])


def view(fx, _args):
    out = fx.view()
    fx.emit("view", device=out["devices"].get(fx.device), acks=out["acks"][-20:], responses=out["responses"][-10:],
            speeches=out["speeches"][-5:])


SCENARIOS = {f.__name__: f for f in (states, approval, clarify, unread, burst, seed, drop, busy, spoken, ui, eight,
                                     closing, chats, view)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="gateway base URL, http://<host>:<port>")
    ap.add_argument("--device", required=True, help="device MAC as on allowed_devices")
    ap.add_argument("--token-env", default="WEIXUE_PROACTIVE_TOKEN", help="environment variable holding the token")
    ap.add_argument("--prefix", help="task id prefix (default: HHMMSS); reuse it for drop")
    ap.add_argument("--log", help="append JSON lines to this file")
    ap.add_argument("--wait", type=float, default=12, help="seconds to wait for acks")
    ap.add_argument("--gap", type=float, default=0.5, help="seconds between items in states")
    ap.add_argument("--busy-delay", type=float, default=3, help="seconds between the conversation and the item in busy")
    ap.add_argument("scenario", choices=sorted(SCENARIOS))
    ap.add_argument("tasks", nargs="*", help="drop: task names (without the prefix)")
    args = ap.parse_args(argv)
    token = os.environ.get(args.token_env)
    if not token:
        raise SystemExit(f"{args.token_env} is not set")
    fx = Fixture(args.base, args.device, token, args.log, args.prefix)
    fx.emit("scenario", name=args.scenario, prefix=fx.prefix, device=fx.device)
    SCENARIOS[args.scenario](fx, args)


if __name__ == "__main__":
    sys.exit(main())
