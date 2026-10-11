#!/usr/bin/env python3
"""Approval-gate rounds on the live agent service, ha-mcp and the board on a diag build: one held
HELD_TOOL call per round (HELD_ENTITY does not exist in Home Assistant) in a new non-current probe chat.

  A approve: one Approve tap, receipt accepted, earcon approved, one replay
  B deny:    one Deny tap, receipt accepted, earcon denied, no replay
  C taps:    two Approve taps, one response sent (the card holds one decision), receipt accepted (any
             later receipt duplicate), one replay
  D expiry:  no tap; waits until the pending row's expires_at plus --expiry-margin, no replay
  P preempt: first a task that asks back (QUESTION_ASK, in its own probe chat) and its question card
             open (by itself, else a tap on the face complication), then the held call: its approval card preempts the question (`card preempted`),
             one Approve tap, receipt accepted, earcon approved, one replay, the question card back

Before each round every open task of a probe chat (title = the start of HELD_ASK or QUESTION_ASK, or a
chat this run created) is cancelled through POST /v1/chats/{id}/interrupt; after the round its own task is settled the
same way. Tasks of other chats are touched only when named with --cancel-task; a message the service routed
into another chat (an open voice window of a question there) ends the round `no_hold` and is not cancelled. A round whose card never
opened on the device is recorded `card_not_opened` with the task of the card that was open, never as run.
Replays are the `ha call finished` lines of HELD_TOOL for the round's task in `docker logs agent-service`
other than outcome `denied`. B runs last by default: a held call identical to a denied one is not held.
An earcon the device skipped while busy (`earcon=<name> skipped: ...`) counts as logged.

Run under the device lock with the board on the diag build:
  .venv/bin/python scripts/approval_gate.py --port <port> --device <mac> --rounds A,C,P,B --out docs/logs/<prefix>
Writes <prefix>.md (table), <prefix>-result.json, <prefix>-serial.log and <prefix>-agent-service.log.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
import types
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "firmware", "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audio_session as a  # noqa: E402
import session_judge as judge  # noqa: E402

ROUNDS = {
    "A": {"name": "approve", "taps": ["approve"], "receipts": ["accepted"], "earcon": "approved", "replays": 1},
    "B": {"name": "deny", "taps": ["deny"], "receipts": ["accepted"], "earcon": "denied", "replays": 0},
    "C": {"name": "duplicate taps", "taps": ["approve", "approve"], "receipts": ["accepted"],
          "earcon": "approved", "replays": 1},
    "D": {"name": "expiry", "taps": [], "receipts": [], "earcon": None, "replays": 0},
    "P": {"name": "preempt", "taps": ["approve"], "receipts": ["accepted"], "earcon": "approved", "replays": 1,
          "question": True},
}
QUESTION_ASK = ("Do not call any tool, not even ask_user. Reply with exactly this question and nothing else: "
                "Which room is this probe for, the kitchen or the bedroom?")
COMPLICATION_TAP = "diag tap 233 268"  # the face complication: opens the top card (the newest question)
TERMINAL = ("done", "failed", "cancelled")
RECEIPT_RE = re.compile(r"WeixueAgent: agent receipt: (approve|deny) seq=\d+ card=(\S+) result=(\w+)")
RESPONSE_RE = re.compile(r"WeixueAgent: agent response: kind=(approve|deny) ")
EARCON_RE = judge.EARCON_RE
CARD_OPEN_RE = re.compile(r"agent ui: card open task=(\S+)$")
ADVANCED_RE = re.compile(r"agent ui: card advanced task=(\S+) by=(\S+) reason=(\S+)")
PREEMPTED_RE = re.compile(r"agent ui: card preempted task=(\S+) by=(\S+)")
PROBE_TITLE_MIN = 12
CARD_WAIT_S = 90  # the WHEN_IDLE gate after the card's speech, or one 20 s auto-advance
RETURN_WAIT_S = 30
VOICE_WINDOW_WAIT_S = 22  # agent-service VOICE_WINDOW_S (20 s): a message sent within it answers the question
QUESTION_AUTO_S = 10  # a question card spoken inline arrives silent: then a tap on the face opens it
RECEIPT_WAIT_S = 15
OUTCOME_WAIT_S = 60
SETTLE_WAIT_S = 10
EXPIRY_MARGIN_S = 30
POLL_S = 2


def held_status(call):
    return f"{a.HELD_TOOL} {call.get('status')}: {(call.get('result_summary') or '')[:80]}"


def is_probe_title(title):
    """Whether a chat title is the start of HELD_ASK or QUESTION_ASK (a chat opened by a probe)."""
    text = (title or "").rstrip("…").rstrip(". ").strip()
    return len(text) >= PROBE_TITLE_MIN and (a.HELD_ASK.startswith(text) or QUESTION_ASK.startswith(text))


def parse_ts(stamp):
    try:
        return datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def count_replays(lines, task_id, tool=a.HELD_TOOL):
    """`ha call finished` lines of tool for task_id, outcome other than denied, in agent-service JSON log lines."""
    n = 0
    for line in lines:
        try:
            rec = json.loads(line[line.index("{"):])
        except ValueError:
            continue
        if rec.get("msg") == "ha call finished" and rec.get("tool") == tool and rec.get("task_id") == task_id \
                and rec.get("outcome") != "denied":
            n += 1
    return n


def docker_logs(since, until, container=a.AGENT_CONTAINER):
    """Redacted agent-service log lines between two epoch seconds, or None."""
    try:
        out = subprocess.run(["docker", "logs", "--since", str(int(since)), "--until", str(int(until) + 1), container],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return [a.redact(l) for l in (out.stdout + out.stderr).splitlines()]


class Gate:
    """Rounds against an agent-service client (request(method, path, body, idempotent) -> (status, body))
    and an audio_session.Session-like console (diag, inject, wait, wait_ready)."""

    def __init__(self, agent, session, device, logs=docker_logs, clock=time.time, sleep=time.sleep,
                 expiry_margin=EXPIRY_MARGIN_S, held_entity=a.HELD_ENTITY):
        self.agent = agent
        self.session = session
        self.diag = session.diag
        self.device = device
        self.logs = logs
        self.clock = clock
        self.sleep = sleep
        self.expiry_margin = expiry_margin
        self.created = []  # probe chats this run opened
        self.held_entity = held_entity
        self.held_ask = a.HELD_ASK.replace(a.HELD_ENTITY, held_entity)
        self.log_lines = []

    def note(self, text):
        self.diag.note(f"gate: {text}")

    # agent-service

    def task(self, task_id):
        status, body = self.agent.request("GET", f"/v1/tasks/{task_id}")
        return body if status == 200 and isinstance(body, dict) else None

    def chats(self):
        status, body = self.agent.request("GET", f"/v1/chats?device_id={self.device}&limit=50")
        return body.get("items") or [] if status == 200 and isinstance(body, dict) else []

    def cancel(self, task_id, named=False):
        """Cancels task_id through its chat's interrupt route while it is that chat's open task, and only in
        a probe chat unless named. Returns {task_id, chat_id, before, action, after}."""
        task = self.task(task_id)
        rec = {"task_id": task_id, "chat_id": (task or {}).get("chat_id"), "before": (task or {}).get("state"),
               "action": "none", "after": None}
        if task is None or task.get("state") in TERMINAL:
            rec["after"] = rec["before"]
            return rec
        status, chat = self.agent.request("GET", f"/v1/chats/{task['chat_id']}")
        if not named and not (task["chat_id"] in self.created or
                              (status == 200 and is_probe_title((chat or {}).get("title")))):
            rec["action"] = "skipped: not a probe chat"
            rec["after"] = rec["before"]
            return rec
        open_task = ((chat or {}).get("open_task") or {}).get("task_id") if status == 200 else None
        if open_task != task_id:
            rec["action"] = f"skipped: open task of the chat is {open_task}"
            rec["after"] = rec["before"]
            return rec
        status, _ = self.agent.request("POST", f"/v1/chats/{task['chat_id']}/interrupt",
                                       {"decision_id": str(uuid.uuid4()), "reason": "api"})
        rec["action"] = f"interrupt HTTP {status}"
        rec["after"] = (self.task(task_id) or {}).get("state")
        self.note(f"cancel {task_id} chat {task['chat_id']}: {rec['before']} -> {rec['after']} ({rec['action']})")
        return rec

    def cleanup(self, extra=()):
        """Cancels every open task of the probe chats, then the tasks named in extra."""
        done = []
        for chat in self.chats():
            if not (is_probe_title(chat.get("title")) or chat.get("chat_id") in self.created):
                continue
            if chat.get("open_task_id"):
                done.append(self.cancel(chat["open_task_id"]))
        for task_id in extra:
            if task_id not in [d["task_id"] for d in done]:
                done.append(self.cancel(task_id, named=True))
        return done

    def ask(self, text, kind, what):
        """New non-current chat, text sent to it, then waits for a pending row of kind. Returns (task_id,
        chat_id, pending row or None, error)."""
        status, chat = self.agent.request("POST", "/v1/chats", {"device_id": self.device, "switch": False},
                                          idempotent=True)
        chat_id = chat.get("chat_id") if isinstance(chat, dict) else None
        if status != 201 or not chat_id:
            return None, None, None, f"chat create answered {status}"
        self.created.append(chat_id)
        status, msg = self.agent.request("POST", f"/v1/chats/{chat_id}/messages",
                                         {"device_id": self.device, "text": text, "lang": "en",
                                          "origin": {"kind": "api"}}, idempotent=True)
        task_id = msg.get("task_id") if isinstance(msg, dict) else None
        if status not in (200, 201) or not task_id:
            return None, chat_id, None, f"message answered {status}"
        if msg.get("chat_id") not in (None, chat_id):
            self.note(f"chat {chat_id}: message routed to chat {msg.get('chat_id')} task {task_id} ({msg.get('routed')})")
            return None, chat_id, None, f"message routed to chat {msg.get('chat_id')} task {task_id} ({msg.get('routed')})"
        self.note(f"chat {chat_id} task {task_id}: {what}")
        deadline = self.clock() + a.HOLD_WAIT_S
        while self.clock() < deadline:
            task = self.task(task_id) or {}
            rows = [p for p in task.get("pending") or [] if p.get("kind") == kind and p.get("state") == "pending"]
            if rows:
                return task_id, chat_id, rows[-1], None
            if task.get("state") in TERMINAL:
                return task_id, chat_id, None, f"task {task['state']} without a pending {kind}"
            self.sleep(POLL_S)
        return task_id, chat_id, None, f"no pending {kind} within {a.HOLD_WAIT_S} s"

    def request_held(self):
        """The held call in a new probe chat: (task_id, chat_id, pending approval row or None, error)."""
        return self.ask(self.held_ask, "approval", f"{a.HELD_TOOL} {self.held_entity}")

    def request_question(self):
        """A task that asks back, in a new probe chat: (task_id, chat_id, pending input row or None, error)."""
        return self.ask(QUESTION_ASK, "input", "ask_user")

    def held_call(self, task_id):
        status, trace = self.agent.request("GET", f"/v1/tasks/{task_id}/trace")
        calls = [c for c in trace.get("tool_calls") or [] if c.get("tool") == a.HELD_TOOL] \
            if status == 200 and isinstance(trace, dict) else []
        return calls[-1] if calls else None

    # device

    def open_card(self):
        """Task of the card open on the device at the end of the console, or None."""
        edges = self.diag.find_all(a.CARD_EDGE_RE, 0)
        if not edges:
            return None
        m = a.CARD_EDGE_RE.search(edges[-1][1])
        return m.group(1) if m else None

    def wait_card(self, task_id, since):
        return self.wait_card_line(task_id, CARD_WAIT_S, since)

    def receipts(self, since):
        return [(n, RECEIPT_RE.search(t).groups()) for _, n, t in self.diag.snapshot(since) if RECEIPT_RE.search(t)]

    def earcons(self, since):
        return [(n, EARCON_RE.search(t).group(1) + EARCON_RE.search(t).group(2))
                for _, n, t in self.diag.snapshot(since) if EARCON_RE.search(t)]

    # rounds

    def run_round(self, key, extra_cancel=()):
        spec = ROUNDS[key]
        out = {"round": key, "name": spec["name"], "run": False, "result": None}
        self.note(f"=== round {key} ({spec['name']})")
        out["cleanup"] = self.cleanup(extra_cancel)
        not_ready = self.session.wait_ready()
        if not_ready:
            out["result"] = f"not_idle: {not_ready.reason}"
            return out
        since = self.diag.mark()
        t0 = self.clock()
        question = None
        if spec.get("question"):
            question = self.open_question(out, since)
            if question is None:
                return out
        task_id, chat_id, row, error = self.request_held()
        out.update(task=task_id, chat=chat_id, pending=row)
        if error:
            call = self.held_call(task_id) if task_id else None
            out["held_call"] = {k: (call or {}).get(k) for k in ("status", "result_summary")}
            out["result"] = f"no_hold: {error}" + (f" ({held_status(call)})" if call else "")
            out["settle"] = self.cancel(task_id) if task_id else None
            self.settle_question(out, question)
            return out
        opened = self.wait_card(task_id, since)
        out["card_open_line"] = opened
        out["advanced"] = [m.groups() for _, _, t in self.diag.snapshot(since) for m in [ADVANCED_RE.search(t)] if m]
        out["preempted"] = [(n, m.groups()) for _, n, t in self.diag.snapshot(since)
                            for m in [PREEMPTED_RE.search(t)] if m]
        if opened is None:
            out["result"] = "card_not_opened"
            out["open_card_task"] = self.open_card()
            out["settle"] = self.cancel(task_id)
            out["replays"] = self.replays(t0, task_id)
            self.settle_question(out, question)
            self.note(f"round {key}: card_not_opened (open card: {out['open_card_task']})")
            return out
        out["run"] = True
        tap_mark = self.diag.mark()
        self.sleep(1.0)
        out["taps"] = []
        for kind in spec["taps"]:
            ok, res, n = self.session.inject(f"diag card {kind}")
            out["taps"].append({"kind": kind, "ok": ok, "line": n})
        if spec["taps"]:
            deadline = self.clock() + RECEIPT_WAIT_S
            while self.clock() < deadline and \
                    len(self.receipts(tap_mark)) < max(1, len(self.diag.find_all(RESPONSE_RE, tap_mark))):
                self.sleep(0.5)
            if spec["earcon"]:
                self.session.wait(rf"FaceDisplay: earcon={spec['earcon']}$", 5, tap_mark)
        out["receipts"] = [{"line": n, "kind": g[0], "card": g[1], "result": g[2]} for n, g in self.receipts(tap_mark)]
        out["responses"] = len(self.diag.find_all(RESPONSE_RE, tap_mark))
        out["earcons"] = [{"line": n, "earcon": e} for n, e in self.earcons(since)]
        if key == "D":
            out["expires_at"] = (row or {}).get("expires_at")
            end = (parse_ts(out["expires_at"]) or self.clock() + 600) + self.expiry_margin
            while self.clock() < end and (self.task(task_id) or {}).get("state") == "needs_approval":
                self.sleep(15)
            out["card_edges"] = [t[-80:] for _, t in self.diag.find_all(a.CARD_EDGE_RE, since)][-3:]
        else:
            self.wait_outcome(task_id, spec)
        if question is not None:
            out["question"]["returned_line"] = self.wait_card_line(question, RETURN_WAIT_S, tap_mark)
        call = self.held_call(task_id) or {}
        out["held_call"] = {k: call.get(k) for k in ("status", "approval_decision", "result_summary")}
        task = self.task(task_id) or {}
        out["end_state"] = task.get("state")
        out["settle"] = self.cancel(task_id)
        out["replays"] = self.replays(t0, task_id)
        self.settle_question(out, question)
        out["result"] = self.verdict(spec, out)
        self.note(f"round {key}: {out['result']}")
        return out

    def open_question(self, out, since):
        """Round P's first step: a task that asks back and its card open on the device. Returns its task
        id, or None with out["result"] set."""
        task_id, chat_id, row, error = self.request_question()
        out["question"] = {"task": task_id, "chat": chat_id, "pending": row, "opened_by": "auto"}
        if error:
            out["result"] = f"no_question: {error}"
            self.settle_question(out, task_id)
            return None
        line = self.wait_card_line(task_id, QUESTION_AUTO_S, since)
        if line is None and not self.session.wait_ready():
            out["question"]["opened_by"] = "complication tap"
            self.session.inject(COMPLICATION_TAP)
            line = self.wait_card_line(task_id, QUESTION_AUTO_S, since)
        out["question"]["card_open_line"] = line
        if line is not None:
            self.sleep(VOICE_WINDOW_WAIT_S)
        if out["question"]["card_open_line"] is None:
            out["result"] = "card_not_opened"
            out["open_card_task"] = self.open_card()
            self.settle_question(out, task_id)
            self.note(f"question card not opened (open card: {out['open_card_task']})")
            return None
        return task_id

    def settle_question(self, out, task_id):
        if task_id:
            out["question"]["state"] = (self.task(task_id) or {}).get("state")
            out["question"]["settle"] = self.cancel(task_id)

    def wait_card_line(self, task_id, timeout, since):
        hit = self.session.wait(judge.card_open_re(task_id), timeout, since)
        return hit[0] if hit else None

    def wait_outcome(self, task_id, spec):
        """Waits for the held call to leave pending (approve) or for SETTLE_WAIT_S (deny)."""
        if spec["replays"] == 0:
            self.sleep(SETTLE_WAIT_S)
            return
        deadline = self.clock() + OUTCOME_WAIT_S
        while self.clock() < deadline:
            status = (self.held_call(task_id) or {}).get("status")
            if status not in (None, "pending", "running"):
                return
            self.sleep(POLL_S)

    def replays(self, t0, task_id):
        lines = self.logs(t0 - 2, self.clock())
        if lines is None:
            return None
        self.log_lines += lines
        return count_replays(lines, task_id)

    @staticmethod
    def verdict(spec, out):
        problems = []
        got = [r["result"] for r in out.get("receipts") or []]
        want = spec["receipts"]
        if got[:len(want)] != want or any(r != "duplicate" for r in got[len(want):]):
            problems.append(f"receipts {got} (expected {want}, later ones duplicate)")
        if spec["taps"] and out.get("responses") != 1:
            problems.append(f"{out.get('responses')} responses sent (expected 1)")
        names = [e["earcon"].split()[0] for e in out.get("earcons") or []]
        if "needs_you" not in names:
            problems.append("no needs_you earcon")
        if spec["earcon"] and spec["earcon"] not in names:
            problems.append(f"no {spec['earcon']} earcon")
        if out.get("replays") is not None and out["replays"] != spec["replays"]:
            problems.append(f"replays {out['replays']} (expected {spec['replays']})")
        if spec.get("question"):
            q = out.get("question") or {}
            if (q.get("task"), out.get("task")) not in [tuple(g) for _, g in out.get("preempted") or []]:
                problems.append("no card preempted line for the question and the approval")
            if not q.get("returned_line"):
                problems.append("the question card did not return")
        return "pass" if not problems else "FAIL: " + "; ".join(problems)


def write_report(path, results, build):
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Approval gate, {datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d %H:%M} UTC\n\n")
        f.write(f"Build: {build or 'diag'}. Held call: `{a.HELD_TOOL}` input_boolean.toggle on `{a.HELD_ENTITY}`. "
                "`L<n>` = serial log line.\n\n")
        f.write("| Round | Card | Taps (responses sent) | Receipts | Earcons | End state (settled) | Replays | Result |\n"
                "|---|---|---|---|---|---|---|---|\n")
        for r in results:
            card = f"L{r['card_open_line']}" if r.get("card_open_line") else \
                (f"not opened (open: {r.get('open_card_task')})" if r.get("result") == "card_not_opened" else "-")
            q = r.get("question")
            if q:
                pre = ", ".join(f"L{n}" for n, _ in r.get("preempted") or []) or "none"
                card = (f"question {q.get('task')} L{q.get('card_open_line') or '-'}; approval {card}; "
                        f"preempted {pre}; question back L{q.get('returned_line') or '-'}")
                if q.get("settle"):
                    card += f"; question {q['settle'].get('before')} -> {q['settle'].get('after')}"
            taps = ", ".join(t["kind"] for t in r.get("taps") or []) or "-"
            if r.get("taps"):
                taps += f" ({r.get('responses')})"
            receipts = ", ".join(f"{x['result']} L{x['line']}" for x in r.get("receipts") or []) or "-"
            earcons = ", ".join(f"{e['earcon']} L{e['line']}" for e in r.get("earcons") or []) or "-"
            settle = (r.get("settle") or {}).get("after")
            end = f"{r.get('end_state') or '-'} ({settle or '-'})"
            f.write(f"| {r['round']} {r['name']} | {card} | {taps} | {receipts} | {earcons} | {end} | "
                    f"{r.get('replays', '-')} | {a.cell(str(r['result']))} |\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True)
    ap.add_argument("--device", required=True, help="device MAC as on allowed_devices")
    ap.add_argument("--rounds", default="A,C,P,B", help="comma-separated subset of A,B,C,D,P, in order")
    ap.add_argument("--out", required=True, help="output prefix (docs/logs/approval-gate-<date>)")
    ap.add_argument("--cancel-task", action="append", default=[],
                    help="also cancel this task before the first round (any chat); repeatable")
    ap.add_argument("--expiry-margin", type=float, default=EXPIRY_MARGIN_S)
    ap.add_argument("--build-info", default="")
    ap.add_argument("--held-entity", default=a.HELD_ENTITY,
                    help="non-existent entity of the held call (another one after a recent deny of the same call)")
    args = ap.parse_args()
    rounds = [r.strip().upper() for r in args.rounds.split(",") if r.strip()]
    unknown = [r for r in rounds if r not in ROUNDS]
    if unknown:
        raise SystemExit(f"unknown rounds: {', '.join(unknown)}")

    from diag_inject import DiagSerial

    diag = DiagSerial(args.port, args.out + "-serial.log", reset=False)
    session = a.Session(types.SimpleNamespace(count=1, no_audio=True, voice=None, barge_in=0), diag)
    gate = Gate(session.agent, session, args.device.lower(), expiry_margin=args.expiry_margin,
                held_entity=args.held_entity)
    results = []
    try:
        for i, key in enumerate(rounds):
            results.append(gate.run_round(key, args.cancel_task if i == 0 else ()))
    finally:
        diag.close()
    with open(args.out + "-result.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1, default=str)
    with open(args.out + "-agent-service.log", "w", encoding="utf-8") as f:
        f.write("\n".join(dict.fromkeys(gate.log_lines)) + "\n")
    write_report(args.out + ".md", results, args.build_info)
    print(f"results: {args.out}.md")
    return 0 if all(r["result"] == "pass" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
