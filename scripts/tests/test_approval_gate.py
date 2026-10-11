"""Host tests for scripts/approval_gate.py with a fake agent service and a fake device console."""
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent / "firmware" / "tools"))

try:
    import approval_gate as g
except ImportError:  # no pyserial
    g = None

DEVICE = "aa:bb:cc:dd:ee:ff"
PROBE_TITLE = "Please call ha_call_service wit…"


class Clock:
    def __init__(self):
        self.t = 1_791_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeAgent:
    """Chats and tasks of the agent service; a message opens a held approval in its chat."""

    def __init__(self, clock, ttl_s=600):
        self.clock = clock
        self.ttl_s = ttl_s
        self.chats = {}
        self.tasks = {}
        self.calls = []
        self.on_message = None
        self.n = 0

    def chat(self, chat_id, title, open_task=None, state="needs_input"):
        self.chats[chat_id] = {"chat_id": chat_id, "title": title, "open_task_id": open_task}
        if open_task:
            self.tasks[open_task] = {"task_id": open_task, "chat_id": chat_id, "state": state, "pending": []}

    def request(self, method, path, body=None, idempotent=False):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/v1/chats?"):
            return 200, {"items": list(self.chats.values())}
        m = re.fullmatch(r"/v1/chats/([^/]+)", path)
        if method == "GET" and m:
            chat = self.chats[m.group(1)]
            task = self.tasks.get(chat["open_task_id"])
            open_task = task if task and task["state"] not in g.TERMINAL else None
            return 200, {"chat_id": chat["chat_id"], "title": chat["title"], "open_task": open_task}
        m = re.fullmatch(r"/v1/chats/([^/]+)/interrupt", path)
        if method == "POST" and m:
            task = self.tasks.get(self.chats[m.group(1)]["open_task_id"])
            if task and task["state"] not in g.TERMINAL:
                task["state"] = "cancelled"
            return 200, {"chat_id": m.group(1)}
        if method == "POST" and path == "/v1/chats":
            self.n += 1
            chat_id = f"cht_probe{self.n}"
            self.chats[chat_id] = {"chat_id": chat_id, "title": "New chat", "open_task_id": None}
            return 201, {"chat_id": chat_id}
        m = re.fullmatch(r"/v1/chats/([^/]+)/messages", path)
        if method == "POST" and m:
            task_id = f"tsk_probe{self.n}"
            chat = self.chats[m.group(1)]
            chat.update(title=PROBE_TITLE, open_task_id=task_id)
            expires = self.clock() + self.ttl_s
            stamp = __import__("datetime").datetime.fromtimestamp(expires, __import__("datetime").timezone.utc)
            kind = "input" if body["text"] == g.QUESTION_ASK else "approval"
            self.tasks[task_id] = {"task_id": task_id, "chat_id": chat["chat_id"],
                                   "state": "needs_input" if kind == "input" else "needs_approval",
                                   "pending": [{"kind": kind, "state": "pending", "interaction_id": f"int_{self.n}",
                                                "expires_at": stamp.isoformat().replace("+00:00", "Z")}],
                                   "calls": []}
            if self.on_message:
                self.on_message(task_id)
            return 201, {"task_id": task_id}
        m = re.fullmatch(r"/v1/tasks/([^/]+)", path)
        if method == "GET" and m:
            task = self.tasks.get(m.group(1))
            return (200, task) if task else (404, None)
        m = re.fullmatch(r"/v1/tasks/([^/]+)/trace", path)
        if method == "GET" and m:
            return 200, {"tool_calls": self.tasks[m.group(1)].get("calls", [])}
        return 404, None

    def interrupted(self):
        return [p.split("/")[3] for m, p, _ in self.calls if p.endswith("/interrupt")]


class FakeDiag:
    def __init__(self):
        self.lines = []
        self.notes = []

    def add(self, text):
        self.lines.append((0.0, len(self.lines) + 1, text))

    def mark(self):
        return len(self.lines)

    def snapshot(self, since=0, until=None):
        return list(self.lines[since:until])

    def find_all(self, pattern, since=0, until=None):
        rx = re.compile(pattern) if isinstance(pattern, str) else pattern
        return [(n, t) for _, n, t in self.lines[since:until] if rx.search(t)]

    def wait_for(self, pattern, timeout, since=None):
        rx = re.compile(pattern) if isinstance(pattern, str) else pattern
        for t, n, text in self.lines[since or 0:]:
            m = rx.search(text)
            if m:
                return n, text, m, t, n
        return None

    def note(self, text):
        self.notes.append(text)


class FakeSession:
    """The device: a tap on a shown card sends one response; the receipt is `receipt`."""

    def __init__(self, diag, receipt="accepted", earcon="approved"):
        self.diag = diag
        self.receipt = receipt
        self.earcon = earcon
        self.decided = False
        self.injected = []
        self.on_tap = None

    def wait_ready(self):
        return None

    def wait(self, pattern, timeout, since=None):
        return self.diag.wait_for(pattern, timeout, since)

    def inject(self, line):
        self.injected.append(line)
        if line.startswith("diag tap"):
            if self.on_tap:
                self.on_tap()
            return True, "ok", len(self.diag.lines)
        kind = line.split()[-1]
        if not self.decided:
            self.decided = True
            self.diag.add(f"WeixueAgent: agent response: kind={kind} chat=c task=t card=int_1 option={kind} via=tap seq=3")
            self.diag.add(f"WeixueAgent: agent receipt: {kind} seq=3 card=int_1 result={self.receipt}")
            self.diag.add(f"FaceDisplay: earcon={'approved' if kind == 'approve' else 'denied'}")
        return True, "ok", len(self.diag.lines)


def finished(task_id, tool="ha_call_service"):
    return "agent-service | " + json.dumps({"msg": "ha call finished", "tool": tool, "task_id": task_id})


@unittest.skipIf(g is None, "pyserial missing")
class ApprovalGateTest(unittest.TestCase):
    def rig(self, opens=True, replays=1, end_state="needs_input"):
        clock = Clock()
        agent = FakeAgent(clock)
        diag = FakeDiag()
        session = FakeSession(diag)

        def on_message(task_id):
            diag.add("FaceDisplay: earcon=needs_you")
            if opens:
                diag.add(f"FaceDisplay: agent ui: card open task={task_id}")
            agent.tasks[task_id]["calls"] = [{"tool": "ha_call_service", "status": "failed" if replays else "denied"}]
            agent.tasks[task_id]["state"] = end_state

        agent.on_message = on_message
        logs = lambda since, until: [finished(t) for t in agent.tasks if t.startswith("tsk_probe")] * replays  # noqa: E731
        gate = g.Gate(agent, session, DEVICE, logs=logs, clock=clock, sleep=clock.sleep)
        return gate, agent, diag, session

    def test_probe_titles(self):
        self.assertTrue(g.is_probe_title(PROBE_TITLE))
        self.assertTrue(g.is_probe_title(g.a.HELD_ASK))
        self.assertTrue(g.is_probe_title(g.QUESTION_ASK[:30] + "…"))
        self.assertFalse(g.is_probe_title("What time is it?"))
        self.assertFalse(g.is_probe_title("Please"))
        self.assertFalse(g.is_probe_title(None))

    def test_cleanup_cancels_only_probe_chat_tasks_and_named_ones(self):
        gate, agent, _, _ = self.rig()
        agent.chat("cht_p", PROBE_TITLE, "tsk_p")
        agent.chat("cht_other", "What time is it?", "tsk_other")
        agent.chat("cht_idle", PROBE_TITLE)
        done = gate.cleanup()
        self.assertEqual(agent.interrupted(), ["cht_p"])
        self.assertEqual([(d["task_id"], d["after"]) for d in done], [("tsk_p", "cancelled")])
        self.assertEqual(agent.tasks["tsk_other"]["state"], "needs_input", "another chat's task is never touched")
        gate.cleanup(["tsk_other"])
        self.assertEqual(agent.tasks["tsk_other"]["state"], "cancelled", "a named task is cancelled")

    def test_cancel_skips_a_task_that_is_not_its_chats_open_task(self):
        gate, agent, _, _ = self.rig()
        agent.chat("cht_p", PROBE_TITLE, "tsk_new")
        agent.tasks["tsk_old"] = {"task_id": "tsk_old", "chat_id": "cht_p", "state": "needs_input"}
        rec = gate.cancel("tsk_old")
        self.assertTrue(rec["action"].startswith("skipped") and agent.interrupted() == [])
        self.assertEqual(gate.cancel("tsk_missing")["action"], "none")

    def test_round_a_passes_and_settles_its_task(self):
        gate, agent, _, session = self.rig()
        agent.chat("cht_stale", PROBE_TITLE, "tsk_stale")
        out = gate.run_round("A")
        self.assertEqual(out["result"], "pass", out)
        self.assertTrue(out["run"] and out["card_open_line"])
        self.assertEqual(session.injected, ["diag card approve"])
        self.assertEqual([r["result"] for r in out["receipts"]], ["accepted"])
        self.assertEqual(out["replays"], 1)
        self.assertEqual(out["cleanup"][0]["task_id"], "tsk_stale", "the stale probe task went first")
        self.assertEqual((out["end_state"], out["settle"]["after"]), ("needs_input", "cancelled"))

    def test_card_not_opened_is_never_reported_as_run(self):
        gate, agent, diag, session = self.rig(opens=False, replays=0)
        diag.add("FaceDisplay: agent ui: card open task=tsk_question")
        out = gate.run_round("B")
        self.assertEqual(out["result"], "card_not_opened")
        self.assertFalse(out["run"])
        self.assertEqual(out["open_card_task"], "tsk_question")
        self.assertEqual(session.injected, [], "no tap without the card")
        self.assertEqual(out["settle"]["after"], "cancelled")

    def test_round_b_deny_expects_no_replay(self):
        gate, _, _, _ = self.rig(replays=0)
        out = gate.run_round("B")
        self.assertEqual(out["result"], "pass", out)
        gate, _, _, _ = self.rig(replays=1)
        self.assertIn("replays 1 (expected 0)", gate.run_round("B")["result"])

    def test_round_c_two_taps_one_response(self):
        gate, _, _, session = self.rig()
        out = gate.run_round("C")
        self.assertEqual(session.injected, ["diag card approve", "diag card approve"])
        self.assertEqual((out["responses"], out["result"]), (1, "pass"))
        gate, _, diag, _ = self.rig()
        gate.session.inject = lambda line: (diag.add("WeixueAgent: agent receipt: approve seq=4 card=int_1 result=accepted")
                                            or (True, "ok", 0))
        self.assertIn("receipts ['accepted', 'accepted']", gate.run_round("C")["result"])

    def test_round_d_waits_for_the_rows_expiry(self):
        gate, agent, _, session = self.rig(replays=0, end_state="needs_approval")
        start = gate.clock()
        orig = agent.request

        def request(method, path, body=None, idempotent=False):
            status, out = orig(method, path, body, idempotent)
            task = agent.tasks.get("tsk_probe1")
            if task and gate.clock() >= start + agent.ttl_s and task["state"] == "needs_approval":
                task["state"] = "failed"
            return status, out

        agent.request = request
        out = gate.run_round("D")
        self.assertEqual(session.injected, [])
        self.assertEqual(out["result"], "pass", out)
        self.assertGreaterEqual(gate.clock() - start, agent.ttl_s)
        self.assertLess(gate.clock() - start, agent.ttl_s + g.EXPIRY_MARGIN_S + 60)
        self.assertEqual(out["end_state"], "failed")

    def preempt_rig(self, preempts=True, returns=True, question_opens=True):
        gate, agent, diag, session = self.rig()
        tasks = []

        def on_message(task_id):
            tasks.append(task_id)
            if agent.tasks[task_id]["pending"][0]["kind"] == "input":
                diag.add("FaceDisplay: earcon=needs_you")
                if question_opens:
                    diag.add(f"FaceDisplay: agent ui: card open task={task_id}")
                return
            diag.add("FaceDisplay: earcon=needs_you")
            if preempts:
                diag.add(f"FaceDisplay: agent ui: card preempted task={tasks[0]} by={task_id}")
                diag.add(f"FaceDisplay: agent ui: card open task={task_id}")
            agent.tasks[task_id]["calls"] = [{"tool": "ha_call_service", "status": "failed"}]
            agent.tasks[task_id]["state"] = "progress"

        agent.on_message = on_message
        orig = session.inject

        def inject(line):
            r = orig(line)
            if returns and line.startswith("diag card"):
                diag.add("FaceDisplay: agent ui: card advanced task=none by=%s reason=return" % tasks[0])
                diag.add(f"FaceDisplay: agent ui: card open task={tasks[0]}")
            return r

        session.inject = inject
        gate.logs = lambda since, until: [finished(t) for t in tasks[1:]]
        return gate, agent, diag, session

    def test_round_p_preempts_the_question_and_settles_both_tasks(self):
        gate, agent, _, session = self.preempt_rig()
        out = gate.run_round("P")
        self.assertEqual(out["result"], "pass", out)
        q, a = out["question"]["task"], out["task"]
        self.assertEqual(out["preempted"][0][1], (q, a))
        self.assertTrue(out["question"]["card_open_line"] and out["question"]["returned_line"])
        self.assertEqual(session.injected, ["diag card approve"])
        self.assertEqual(out["replays"], 1)
        self.assertEqual((out["settle"]["after"], out["question"]["settle"]["after"]), ("cancelled", "cancelled"))
        self.assertEqual(set(agent.interrupted()), {agent.tasks[q]["chat_id"], agent.tasks[a]["chat_id"]})

    def test_round_p_fails_without_preemption_or_return(self):
        out = self.preempt_rig(preempts=False)[0].run_round("P")
        self.assertEqual(out["result"], "card_not_opened", "the approval card never opened")
        self.assertEqual(out["question"]["settle"]["after"], "cancelled")
        out = self.preempt_rig(returns=False)[0].run_round("P")
        self.assertIn("the question card did not return", out["result"])

    def test_round_p_stops_when_the_question_card_does_not_open(self):
        gate, agent, diag, session = self.preempt_rig(question_opens=False)
        diag.add("FaceDisplay: agent ui: card open task=tsk_blocking")
        out = gate.run_round("P")
        self.assertEqual((out["result"], out["open_card_task"], out["run"]), ("card_not_opened", "tsk_blocking", False))
        self.assertNotIn("task", out, "no held call without the question card")
        self.assertEqual(out["question"]["settle"]["after"], "cancelled")
        self.assertEqual(session.injected, [g.COMPLICATION_TAP], "one tap on the face, no card tap")

    def test_round_p_opens_a_silent_question_card_with_a_face_tap(self):
        gate, agent, diag, session = self.preempt_rig(question_opens=False)
        session.on_tap = lambda: diag.add(f"FaceDisplay: agent ui: card open task={agent.tasks and sorted(agent.tasks)[0]}")
        out = gate.run_round("P")
        self.assertEqual((out["result"], out["question"]["opened_by"]), ("pass", "complication tap"), out)

    def test_a_message_routed_into_another_chat_is_not_cancelled(self):
        gate, agent, _, _ = self.rig()
        agent.chat("cht_user", "Jarvis. Jarvis.", "tsk_user")
        orig = agent.request

        def request(method, path, body=None, idempotent=False):
            if method == "POST" and path.endswith("/messages"):
                agent.calls.append((method, path, body))
                return 200, {"chat_id": "cht_user", "task_id": "tsk_user", "routed": "answer"}
            return orig(method, path, body, idempotent)

        agent.request = request
        out = gate.run_round("A")
        self.assertTrue(out["result"].startswith("no_hold: message routed to chat cht_user"), out)
        self.assertEqual(agent.tasks["tsk_user"]["state"], "needs_input", "another chat's task is never cancelled")
        self.assertEqual(gate.cancel("tsk_user")["action"], "skipped: not a probe chat")

    def test_count_replays(self):
        lines = [finished("t1"), finished("t2"), finished("t1", "ha_get_state"), "not json", finished("t1")]
        self.assertEqual(g.count_replays(lines, "t1"), 2)
        denied = "agent-service | " + json.dumps({"msg": "ha call finished", "tool": "ha_call_service",
                                                  "task_id": "t1", "outcome": "denied"})
        self.assertEqual(g.count_replays([denied], "t1"), 0, "a denied call is not a replay")

    def test_an_earcon_skipped_while_busy_counts_as_logged(self):
        gate, _, diag, _ = self.rig()
        orig = gate.session.inject

        def inject(line):
            r = orig(line)
            diag.lines = [(t, n, x.replace("earcon=approved", "earcon=approved skipped: device busy or muted"))
                          for t, n, x in diag.lines]
            return r

        gate.session.inject = inject
        out = gate.run_round("A")
        self.assertEqual(out["result"], "pass", out)

    def test_unknown_rounds_are_refused(self):
        with mock.patch.object(sys, "argv", ["approval_gate.py", "--port", "p", "--device", DEVICE,
                                             "--out", "x", "--rounds", "A,E"]):
            with self.assertRaises(SystemExit):
                g.main()


if __name__ == "__main__":
    unittest.main()
