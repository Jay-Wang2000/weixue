"""server/scripts/agent_msg_fixture.py scenarios: no item carries a scripted answer (ADR-13)."""
import importlib.util
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixture = load("agent_msg_fixture")


class Recorder(fixture.Fixture):
    def __init__(self):
        super().__init__("http://gateway", "aa:bb:cc:dd:ee:01", "token", prefix="p")
        self.items = []

    def emit(self, event, **data):
        pass

    def send(self, items, drop=None):
        self.items += items
        return {"items": [{"delivery_id": f"d{i}", "task_id": it.get("task_id")} for i, it in enumerate(items)]}

    def announce(self, message, mode="announce", urgent=False):
        return {}

    def report(self, records, wait, responses=0, final=False):
        pass

    def request(self, method, path, body=None, headers=None):
        return 200, {"devices": {}, "acks": [], "responses": [], "speeches": []}


ARGS = types.SimpleNamespace(wait=0, gap=0, busy_delay=0, tasks=["keep"])


@pytest.mark.parametrize("name", sorted(fixture.SCENARIOS))
def test_scenarios_send_no_scripted_answer(name, monkeypatch):
    monkeypatch.setattr(fixture.time, "sleep", lambda s: None)
    fx = Recorder()
    fixture.SCENARIOS[name](fx, ARGS)
    for it in fx.items:
        assert "debug_reply" not in it and "debug_reply" not in it.get("raw", {})


def test_card_scenarios_still_send_cards():
    for name, state in (("approval", "needs_approval"), ("clarify", "needs_input"), ("closing", "needs_approval")):
        fx = Recorder()
        fixture.SCENARIOS[name](fx, ARGS)
        assert [it["state"] for it in fx.items if "card_id" in it] == [state], name


def test_sim_device_taps_like_a_finger():
    sim = load("agent_sim_device")
    dev = sim.SimDevice.__new__(sim.SimDevice)
    dev.a = types.SimpleNamespace(tap="approve")
    card = {"chat_id": "c", "task_id": "t", "state": "needs_approval", "card_id": "i1",
            "options": [{"id": "approve", "label": "Approve"}, {"id": "deny", "label": "Deny"}]}
    assert dev.tap(card) == {"chat_id": "c", "task_id": "t", "via": "tap", "kind": "approve", "card_id": "i1",
                             "option_id": "approve"}
    question = dict(card, state="needs_input", options=[{"id": "hall", "label": "Hall"}])
    assert dev.tap(question)["kind"] == "answer" and dev.tap(question)["option_id"] == "hall"
    assert dev.tap({"chat_id": "c", "task_id": "t", "state": "unread"})["kind"] == "ack_seen"
    assert dev.tap({"chat_id": "c", "task_id": "t", "state": "working"}) is None
