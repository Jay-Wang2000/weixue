#!/usr/bin/env python3
"""Approval-gate rounds B (deny), C (duplicate taps) and D (expiry) against the live agent service,
ha-mcp and the board on the diag build. Reuses scripts/audio_session.py helpers.
Run under the device lock: .venv/bin/python docs/logs/approval-gate-2026-10-11-driver.py <port> <device> <rounds> <out-prefix>
"""
import json
import re
import subprocess
import sys
import time
import types

ROOT = "/Users/jwang14/Projects/weixue"
sys.path.insert(0, ROOT + "/scripts")
sys.path.insert(0, ROOT + "/firmware/tools")
import audio_session as a  # noqa: E402
from diag_inject import DiagSerial  # noqa: E402

port, device, rounds, prefix = sys.argv[1], sys.argv[2].lower(), sys.argv[3], sys.argv[4]
diag = DiagSerial(port, prefix + "-serial.log", reset=False)
args = types.SimpleNamespace(count=1, no_audio=True, voice=None, barge_in=0)
s = a.Session(args, diag)
ag = s.agent
T0 = time.time()
RECEIPT = re.compile(r"WeixueAgent: agent receipt: (approve|deny) seq=(\d+) card=(\S+) result=(\w+)")
out = {}


def log(msg):
    line = f"[{time.time() - T0:7.1f}s {time.strftime('%H:%M:%SZ', time.gmtime())}] {msg}"
    print(line, flush=True)
    diag.note(msg)


def request_held():
    """New non-current chat + the held call. Returns (task_id, chat_id, t_actionable, pending_row)."""
    st, chat = ag.request("POST", "/v1/chats", {"device_id": device, "switch": False}, idempotent=True)
    chat_id = chat.get("chat_id")
    st, msg = ag.request("POST", f"/v1/chats/{chat_id}/messages",
                         {"device_id": device, "text": a.HELD_ASK, "lang": "en", "origin": {"kind": "api"}},
                         idempotent=True)
    task_id = msg.get("task_id")
    log(f"chat {chat_id} task {task_id} requested (message HTTP {st})")
    row = None
    for _ in range(60):
        _, task = ag.request("GET", f"/v1/tasks/{task_id}")
        task = task if isinstance(task, dict) else {}
        holds = [p for p in task.get("pending") or [] if p.get("kind") == "approval"]
        if any(p.get("state") == "pending" for p in holds):
            row = holds[-1]
            log(f"task state {task.get('state')}, approval pending: {json.dumps(row)[:400]}")
            return task_id, chat_id, time.time(), row
        time.sleep(1)
    log("NO approvable hold in 60 s")
    return task_id, chat_id, None, None


def wait_card(task_id, since, timeout=30):
    hit = s.wait(a.judge.card_open_re(task_id), timeout, since)
    log(f"card open on device: {'L%s' % hit[0] if hit else 'NOT seen'}")
    return hit


def snapshot(task_id, chat_id, tag):
    _, task = ag.request("GET", f"/v1/tasks/{task_id}")
    _, trace = ag.request("GET", f"/v1/tasks/{task_id}/trace")
    _, chats = ag.request("GET", f"/v1/chats?device_id={device}")
    calls = [{k: c.get(k) for k in ("tool", "status", "approval_decision", "result_summary")}
             for c in (trace or {}).get("tool_calls") or []] if isinstance(trace, dict) else trace
    chat = next((c for c in (chats or {}).get("items", []) if c["chat_id"] == chat_id), None) if isinstance(chats, dict) else None
    info = {"task_state": (task or {}).get("state"), "pending": (task or {}).get("pending"),
            "tool_calls": calls, "chat": chat}
    log(f"{tag}: {json.dumps(info)[:1500]}")
    return info


def receipts(since):
    return [(n, RECEIPT.search(t).groups()) for _, n, t in diag.snapshot(since) if RECEIPT.search(t)]


if "B" in rounds:
    log("=== ROUND B: deny")
    since = diag.mark()
    task_id, chat_id, t_act, row = request_held()
    out["B"] = {"task": task_id, "chat": chat_id}
    if t_act and wait_card(task_id, since):
        time.sleep(1)
        tap = diag.mark()
        t_tap = time.time()
        ok, res, n = s.inject("diag card deny")
        log(f"diag card deny: ok={ok} {res} (L{n})")
        hit = s.wait(r"agent receipt: deny ", 15, tap)
        t_rcpt = time.time()
        log(f"receipt: {hit[1][:200] if hit else 'NONE'} ({t_rcpt - t_tap:.2f} s after tap)")
        closed = s.wait(r"agent ui: card closed", 10, tap)
        log(f"card closed: {'L%s' % closed[0] if closed else 'NOT seen'}")
        time.sleep(8)
        out["B"]["after_8s"] = snapshot(task_id, chat_id, "service state 8 s after deny")
        d = s.inject("diag dump")
        log(f"diag dump: {d[1][:700]}")
        time.sleep(25)
        out["B"]["after_33s"] = snapshot(task_id, chat_id, "service state 33 s after deny")
    log("=== ROUND B end")

if "C" in rounds:
    log("=== ROUND C: duplicate taps")
    since = diag.mark()
    task_id, chat_id, t_act, row = request_held()
    out["C"] = {"task": task_id, "chat": chat_id}
    if t_act and wait_card(task_id, since):
        time.sleep(1)
        tap = diag.mark()
        t1 = time.time()
        ok1, res1, n1 = s.inject("diag card approve")
        t2 = time.time()
        ok2, res2, n2 = s.inject("diag card approve")
        t3 = time.time()
        log(f"tap1: ok={ok1} {res1} (L{n1}) at +0.00 s; tap2: ok={ok2} {res2} (L{n2}) at +{t2 - t1:.2f} s (returned +{t3 - t1:.2f} s)")
        time.sleep(10)
        for n, g in receipts(tap):
            log(f"receipt L{n}: {g}")
        out["C"]["receipts"] = [g for _, g in receipts(tap)]
        out["C"]["after"] = snapshot(task_id, chat_id, "service state after taps")
        time.sleep(15)
        out["C"]["after_25s"] = snapshot(task_id, chat_id, "service state 25 s after taps")
    log("=== ROUND C end")

if "D" in rounds:
    log("=== ROUND D: expiry")
    since = diag.mark()
    task_id, chat_id, t_act, row = request_held()
    out["D"] = {"task": task_id, "chat": chat_id, "row": row}
    if t_act and wait_card(task_id, since):
        exp = (row or {}).get("expires_at")
        log(f"no tap; interaction expires_at {exp}")
        deadline = time.time() + float(sys.argv[5] if len(sys.argv) > 5 else 780)
        closed = None
        last = None
        while time.time() < deadline:
            time.sleep(15)
            _, task = ag.request("GET", f"/v1/tasks/{task_id}")
            st = (task or {}).get("state") if isinstance(task, dict) else None
            if st != last:
                log(f"task state {st}")
                last = st
            if st in ("expired", "failed", "done", "cancelled", "interrupted", "stopped"):
                break
        time.sleep(10)
        closed = diag.find_all(a.CARD_EDGE_RE, since)
        log(f"card edges after: {[ (n, t[-60:]) for n, t in closed][-3:]}")
        out["D"]["after"] = snapshot(task_id, chat_id, "service state at end of wait")
        d = s.inject("diag dump")
        log(f"diag dump: {d[1][:700]}")
    log("=== ROUND D end")

json.dump(out, open(prefix + "-result.json", "w"), indent=1, default=str)
diag.close()
