"""End-to-end conversation gate of feat-agent-service (brief section 13) against the live service, ha-mcp and
the Anthropic API, with tools.sim_device as gateway and device. Every expectation is asserted; a failure exits
non-zero. Read-only HA entities, the held probe on a non-existent input_boolean (denied), and
light.living_room_ceiling_lights (restored afterwards).

    python -m tools.e2e_conversation <scenario>... [--state FILE]
Scenarios: short long steer denied free second restart-begin restart-check disconnect barge-in
"""
import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from tools.sim_device import Client, Device  # noqa: E402

ACK_AFTER_S, REPLY_HOLD_S, CANCEL_MAX_S = 20, 6, 2
LONG = ("Look at the living room lights and today's sunset time, check their history this week, and draft "
        "(don't apply) an automation that turns them on at sunset. Don't save anything to memory and don't ask me "
        "anything: just tell me the draft when you're done.")
SLOW = ("For light.living_room_ceiling_lights, light.living_room_dining_ceiling_light, sun.sun, zone.home and "
        "the living room donut light, look at the history of each one for each of the last seven days, one entity and "
        "one day per tool call, one call at a time, waiting for each result before the next, then summarize the "
        "daily patterns. Don't save anything to memory.")
HELD = ("Please call ha_call_service with domain input_boolean, service toggle and entity_id "
        "input_boolean.weixue_e2e_probe. It is a test entity; make the call exactly once.")
LIGHT = "light.living_room_ceiling_lights"
T0 = time.monotonic()


def log(*parts: Any) -> None:
    print(f"{time.monotonic() - T0:8.2f}s", *parts, flush=True)


class Failed(Exception):
    pass


def check(condition: bool, what: str) -> None:
    if not condition:
        raise Failed(what)
    log("  ok:", what)


def spoken(events: List[Dict[str, Any]]) -> str:
    return "".join(e["data"].get("delta", "") for e in events if e["type"] == "text")


class Run:
    def __init__(self, url: str, state_file: Path):
        self.c = Client(url)
        self.device = Device(self.c, log=lambda m: log("  device", m))
        self.device.start()
        self.device.connected.wait(10)
        self.state_file = state_file
        self.results: Dict[str, Any] = {}

    # ------------------------------------------------------------------ helpers

    def task(self, task_id: str) -> Dict[str, Any]:
        return self.c.get(f"/v1/tasks/{task_id}")

    def trace(self, task_id: str) -> Dict[str, Any]:
        return self.c.get(f"/v1/tasks/{task_id}/trace")

    def wait(self, predicate: Callable[[], Any], timeout: float, what: str, every: float = 1.0) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(every)
        raise Failed(f"timed out after {timeout:.0f}s: {what}")

    def wait_state(self, task_id: str, states: tuple, timeout: float) -> Dict[str, Any]:
        return self.wait(lambda: (lambda t: t if t["state"] in states else None)(self.task(task_id)), timeout,
                         f"task {task_id} in {states}")

    def say(self, text: str, chat: str = "current", **kw: Any) -> List[Dict[str, Any]]:
        t = time.monotonic()
        events = self.c.message(text, chat, **kw)
        first_text = next((e["at"] - t for e in events if e["type"] == "text"), None)
        log(f"say {text[:60]!r} -> routed={events[0]['data'].get('routed')} "
            f"end={events[-1]['data'].get('reason') if events[-1]['type'] == 'reply_end' else events[-1]['type']} "
            f"first_text={first_text and round(first_text, 2)}s spoken={spoken(events)[:120]!r}")
        return events

    def ha_calls(self, trace: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [c for c in trace["tool_calls"] if c["kind"] == "ha"]

    def new_chat(self) -> str:
        return self.c.new_chat()["chat_id"]

    def light_state(self) -> Optional[str]:
        import asyncio
        from weixue_hamcp import HaMcpClient

        async def read() -> Optional[str]:
            client = HaMcpClient(os.environ["HA_MCP_URL"])
            try:
                result = await client.call_tool("ha_get_state", {"entity_id": LIGHT, "fields": ["state"]},
                                                read_timeout_s=20)
                text = "\n".join(c.get("text", "") for c in result.get("content") or [])
                data = json.loads(text) if text.strip().startswith("{") else result.get("structuredContent") or {}
                return (data.get("data") or data).get("state")
            finally:
                await client.aclose()
        return asyncio.run(read())

    def settle(self) -> None:
        """Stop work left open by an earlier scenario (its question would catch the next message)."""
        self.device.busy.clear()
        for chat in self.c.get("/v1/chats", device_id=self.c.device, limit=12)["items"]:
            if chat.get("open_task_id"):
                self.c.interrupt(chat["chat_id"], "api")
                log(f"  settle: stopped the open task of {chat['chat_id']}")

    # ------------------------------------------------------------------ scenarios

    def short(self) -> None:
        events = self.say("Is the living room ceiling light on?", self.new_chat())
        task_id = events[0]["task_id"]
        check(events[0]["data"]["routed"] == "new_task", "routed new_task")
        check(events[-1]["data"]["reason"] == "turn_done", "reply_end turn_done")
        task = self.wait_state(task_id, ("done",), 60)
        trace = self.trace(task_id)
        check(all(u["mode"] == "quick" for u in trace["usage"]), "quick mode only")
        reads = self.ha_calls(trace)
        check(1 <= len(reads) and all(c["read_only"] for c in reads), f"HA reads only ({[c['tool'] for c in reads]})")
        done = [e for e in trace["events"] if e["type"] == "done"][0]["data"]
        check(done["speak"] is None, "done.speak null (spoken inline)")
        self.results["short"] = {"service_first_text_ms": trace["turns"][0]["ttft_ms"],
                                 "requests": len(trace["usage"]), "outcome": task["outcome"]}

    def _long(self, chat: str) -> Dict[str, Any]:
        t = time.monotonic()
        events = self.say(LONG, chat)
        elapsed = events[-1]["at"] - t
        check(events[-1]["type"] == "reply_end" and elapsed <= ACK_AFTER_S + REPLY_HOLD_S + 2,
              f"reply_end within ack + hold ({elapsed:.1f}s, {events[-1]['data'].get('reason')})")
        check(bool(spoken(events).strip()), "an acknowledgement was spoken")
        return {"events": events, "task_id": events[0]["task_id"], "t": t}

    def long(self) -> None:
        run = self._long(self.new_chat())
        task_id = run["task_id"]
        self.device.busy.set()
        task = self.wait_state(task_id, ("done", "failed"), 900)
        check(task["state"] == "done", "task done")
        total = time.monotonic() - run["t"]
        trace = self.trace(task_id)
        models = {u["model"] for u in trace["usage"]}
        check("claude-fable-5-1" in models, f"switched to Fable ({sorted(models)})")
        check(len(self.ha_calls(trace)) >= 3, f">=3 HA calls ({len(self.ha_calls(trace))})")
        offers = lambda: [o for o in self.device.offers if o["slot"] == f"task:{task_id}" and o.get("speak")]
        self.wait(offers, 30, "spoken result offered while busy (deferred)")
        cleared = time.monotonic()
        self.device.busy.clear()
        delivered = self.wait(lambda: [o for o in offers() if o["at"] > cleared], 15, "result after un-busying")
        check(delivered[0]["tier"] == "WHEN_IDLE" and delivered[0]["at"] - cleared <= 10,
              f"WHEN_IDLE result within 10 s of idle ({delivered[0]['at'] - cleared:.1f}s)")
        self.results["long"] = {"total_s": round(total, 1), "ha_calls": len(self.ha_calls(trace)),
                                "cost_usd": task["cost_usd"], "over_30s": total > 30}

    def steer(self) -> None:
        chat = self.new_chat()
        events = self.say(SLOW, chat)
        task_id = events[0]["task_id"]
        self.wait(lambda: len(self.ha_calls(self.trace(task_id))) >= 1, 120, "one HA call before the steer")
        before = len(self.trace(task_id)["tool_calls"])
        steer = self.say("How is it going?", chat)
        check(steer[0]["data"]["routed"] == "steer", "routed steer")
        check(bool(spoken(steer).strip()), "a progress reply was spoken")
        interrupted = self.wait(lambda: [e for e in self.trace(task_id)["events"] if e["type"] == "interrupted"], 30,
                                "interrupted event")
        reason = interrupted[0]["data"]["terminal_reason"]
        check(reason == "steered", f"interrupted{{{reason}}} drain_ms={interrupted[0]['data']['drain_ms']}")
        self.wait(lambda: len(self.trace(task_id)["tool_calls"]) > before, 180, "more tool calls after the steer")
        if steer[-1]["data"].get("reason") == "question":
            log("  the steer reply asked a question: waiting out the voice window before the stop")
            time.sleep(21)
        t_stop = time.monotonic()
        stop = self.say("stop", chat)
        check(stop[0]["data"]["routed"] == "cancel", "routed cancel")
        check(spoken(stop).startswith("Stopped."), f"spoke {spoken(stop)!r}")
        task = self.task(task_id)
        check(task["state"] == "cancelled" and time.monotonic() - t_stop <= CANCEL_MAX_S + 3,
              f"cancelled within the cancel bound ({time.monotonic() - t_stop:.1f}s incl. the stream)")
        check("side_effects" in task, f"side effects listed: {task['side_effects']}")
        stopped_at = [e for e in self.trace(task_id)["events"] if e["type"] == "cancelled"][0]["ts"]
        time.sleep(5)
        late = [c for c in self.trace(task_id)["tool_calls"] if c["started_at"] > stopped_at]
        check(not late, "no tool call after the stop")
        self.results["steer"] = {"drain_ms": interrupted[0]["data"]["drain_ms"], "side_effects": task["side_effects"]}

    def denied(self) -> None:
        chat = self.new_chat()
        events = self.say(HELD, chat)
        task_id = events[0]["task_id"]
        self.wait_state(task_id, ("needs_approval",), 60)
        self.wait(lambda: self.task(task_id)["current_turn_id"] is None, 60, "the turn ended (card waits for a tap)")
        pending = self.wait(lambda: [p for p in self.task(task_id)["pending"] if p["kind"] == "approval"
                                     and p["state"] == "pending"], 30, "owned approval card")
        check(pending[0]["source"] == "ha_mcp", "approval from ha-mcp")
        self.c.decide(chat, pending[0]["interaction_id"], "deny")
        task = self.wait_state(task_id, ("done", "failed"), 120)
        trace = self.trace(task_id)
        resolved = [e for e in trace["events"] if e["type"] == "resolved"]
        check(resolved and resolved[0]["data"]["state"] == "denied", "resolved{denied}")
        check(any(t["kind"] == "event" for t in trace["turns"]), "event turn ran")
        check(task["state"] == "done" and task["outcome"] == "denied", f"done{{outcome:{task['outcome']}}}")
        self.results["denied"] = {"outcome": task["outcome"]}

    def free(self) -> None:
        original = self.light_state()
        check(original in ("on", "off"), f"light state read ({original})")
        target = "off" if original == "on" else "on"
        chat = self.new_chat()
        tasks = []
        for state in (target, original):
            events = self.say(f"Turn {state} the living room ceiling lights.", chat)
            task = self.wait_state(events[0]["task_id"], ("done", "failed"), 90)
            tasks.append(task)
            now = self.wait(lambda: (lambda s: s if s == state else None)(self.light_state()), 20, f"light {state}")
            check(now == state, f"ha_get_state confirms {state}")
            effects = task["side_effects"]
            check(any(e["status"] == "confirmed" for e in effects), f"side_effects confirmed: {effects}")
        check(self.light_state() == original, f"state restored ({original})")
        self.results["free"] = {"original": original, "outcomes": [t["outcome"] for t in tasks]}

    def second(self) -> None:
        chat_a = self.new_chat()
        holder: Dict[str, Any] = {}
        thread = threading.Thread(target=lambda: holder.update(task_id=self.say(SLOW, chat_a)[0]["task_id"]),
                                  daemon=True)
        thread.start()
        thread.join(40)
        task_a = holder["task_id"]
        chat_b = self.new_chat()
        busy: List[int] = []
        poller_stop = threading.Event()

        def poll() -> None:
            while not poller_stop.is_set():
                busy.append(sum(1 for a in self.c.get("/v1/status")["actors"] if a["busy"]))
                time.sleep(0.5)
        poller = threading.Thread(target=poll, daemon=True)
        poller.start()
        events = self.say("What's the weather like in Dublin right now?", chat_b)
        poller_stop.set()
        check(events[0]["data"]["routed"] == "new_task" and events[-1]["data"]["reason"] == "turn_done",
              "second chat answered with reply_end turn_done")
        check(self.task(task_a)["state"] in ("progress", "needs_input", "needs_approval"), "chat A still working")
        check(max(busy or [0]) >= 2, f"two busy actors seen in status (max {max(busy or [0])})")
        trace_a = self.trace(task_a)
        check(not [e for e in trace_a["events"] if e["type"] == "interrupted"], "no interrupted in chat A")
        trace_b = self.trace(events[0]["task_id"])
        server = [c["tool"] for c in trace_b["tool_calls"] if c["kind"] == "server"]
        self.results["second"] = {"chat_b_server_tools": server}
        self.c.interrupt(chat_a, "api")

    def restart_begin(self) -> None:
        chat = self.new_chat()
        events = self.say(SLOW, chat)
        run = {"task_id": events[0]["task_id"]}
        self.wait(lambda: len(self.ha_calls(self.trace(run["task_id"]))) >= 2, 120, "work in progress")
        check(self.task(run["task_id"])["current_turn_id"] is not None, "a turn is in flight at the restart")
        listing = self.c.get("/v1/chats", device_id=self.c.device)
        self.state_file.write_text(json.dumps({"chat": chat, "task": run["task_id"],
                                               "chats": [c["chat_id"] for c in listing["items"]],
                                               "rows": len(self.c.get(f"/v1/chats/{chat}")["transcript"])}))
        log("restart now")

    def restart_check(self) -> None:
        state = json.loads(self.state_file.read_text())
        task = self.task(state["task"])
        check(task["state"] == "failed" and task["error"]["code"] == "service_restart", "failed{service_restart}")
        offer = self.wait(lambda: [o for o in self.device.offers if o["slot"] == f"task:{state['task']}"
                                   and o.get("speak")], 30, "restart notice delivered")
        check(offer[0]["tier"] == "WHEN_IDLE" and "restarted" in offer[0]["speak"], f"notice {offer[0]['speak']!r}")
        listing = self.c.get("/v1/chats", device_id=self.c.device)
        check([c["chat_id"] for c in listing["items"]] == state["chats"], "chats list unchanged")
        events = self.say("Did anything interrupt you just now? Answer in one sentence, no question.", state["chat"])
        text = spoken(events).lower()
        rows = self.c.get(f"/v1/chats/{state['chat']}")["transcript"]
        check(len(rows) > state["rows"], f"same transcript continued ({state['rows']} -> {len(rows)} rows)")
        check(any(w in text for w in ("restart", "cut off", "interrupted", "didn't finish", "did not finish", "not finish")),
              f"the reply knows the work was cut off ({text[:80]!r})")
        self.results["restart"] = {"reply": text[:160]}
        self.c.interrupt(state["chat"], "api")

    def disconnect(self) -> None:
        chat = self.new_chat()
        t = time.monotonic()
        events = self.say(SLOW, chat)
        check(events[0]["data"]["routed"] == "new_task", "routed new_task")
        task_id = events[0]["task_id"]
        self.device.stop()
        log("device disconnected")
        task = self.wait_state(task_id, ("done", "failed"), 1200, )
        check(task["state"] == "done", f"task done after {time.monotonic() - t:.0f}s without a device")
        snapshot = self.c.get(f"/v1/devices/{self.c.device}/snapshot")
        cards = [c for c in snapshot["cards"] if c["slot"] == f"task:{task_id}"]
        check(bool(cards), "snapshot carries the result card")
        self.device = Device(self.c, log=lambda m: log("  device", m))
        self.device.start()
        offer = self.wait(lambda: [o for o in self.device.offers if o["slot"] == f"task:{task_id}" and o.get("speak")],
                          30, "WHEN_IDLE result after reconnect")
        check(offer[0]["tier"] == "WHEN_IDLE", "result tier WHEN_IDLE")
        self.results["disconnect"] = {"duration_s": round(time.monotonic() - t), "cost_usd": task["cost_usd"]}

    def barge_in(self) -> None:
        chat = self.new_chat()
        texts: List[Dict[str, Any]] = []
        cut = self.say("Tell me about the history of the Spire in Dublin, in detail.", chat,
                       stop=lambda e: e["type"] == "text" and len(texts.append(e) or texts) >= 2)
        task_id = cut[0]["task_id"]
        check(cut[-1]["type"] == "text", "stream dropped mid-answer")
        correction = self.say("Sorry, I meant the Ha'penny Bridge. Just one sentence.", chat)
        check(correction[0]["data"]["routed"] in ("steer", "continue"), f"correction routed "
              f"{correction[0]['data']['routed']}")
        interrupted = self.wait(lambda: [e for e in self.trace(task_id)["events"] if e["type"] == "interrupted"], 30,
                                "interrupted event")[0]["data"]
        check(interrupted["terminal_reason"] == "barge_in" and (interrupted["drain_ms"] or 0) < 500,
              f"barge-in cancel {interrupted['drain_ms']} ms")
        rows = self.c.get(f"/v1/chats/{chat}")["transcript"]
        check([r["role"] for r in rows][:2] == ["user", "system"] and rows[0]["rewritten"] >= 1,
              "no partial assistant message; the tail was rewritten with the correction")
        check("penny" in spoken(correction).lower() or "ha'penny" in spoken(correction).lower(),
              "the reply follows the correction")
        self.results["barge_in"] = {"cancel_ms": interrupted["drain_ms"]}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("scenarios", nargs="+")
    p.add_argument("--url", default=os.environ.get("AGENT_URL", "http://agent-service:8010"))
    p.add_argument("--state", default="/tmp/e2e-state.json")
    args = p.parse_args()
    run = Run(args.url, Path(args.state))
    failures = 0
    for name in args.scenarios:
        log(f"== {name}")
        try:
            if name != "restart-check":
                run.settle()
            getattr(run, name.replace("-", "_"))()
        except Failed as e:
            failures += 1
            log(f"  FAIL: {e}")
        except Exception as e:
            failures += 1
            log(f"  ERROR: {type(e).__name__}: {str(e)[:300]}")
    log("results", json.dumps(run.results, ensure_ascii=False))
    run.device.stop()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
