"""Simulated gateway + device for agent-service: the provider's message stream, the outbox consumer with
two-phase acks, panel decisions and the chat routes.

    AGENT_URL=http://agent-service:8010 python -m tools.sim_device ask "What's the weather like?"
    python -m tools.sim_device watch --seconds 120 [--busy 20]

The bearer token comes from AGENT_SERVICE_TOKEN, else from the AGENT_SERVICE_TOKEN line of AGENT_ENV_FILE
(default server/.env); it is never printed.
"""
import argparse
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

import httpx

DEVICE = "sim-device-01"


def token() -> str:
    if os.environ.get("AGENT_SERVICE_TOKEN"):
        return os.environ["AGENT_SERVICE_TOKEN"]
    env_file = Path(os.environ.get("AGENT_ENV_FILE", Path(__file__).resolve().parents[2] / ".env"))
    for line in env_file.read_text().splitlines():
        if line.startswith("AGENT_SERVICE_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"')
    sys.exit("AGENT_SERVICE_TOKEN not found")


def parse_sse(lines: Iterator[str]) -> Iterator[Dict[str, Any]]:
    event: Dict[str, Any] = {}
    for line in lines:
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event["event"] = line[6:].strip()
        elif line.startswith("id:"):
            event["id"] = line[3:].strip()
        elif line.startswith("data:"):
            event["data"] = json.loads(line[5:].strip() or "{}")
        elif line == "" and "data" in event:
            yield event
            event = {}


class Client:
    def __init__(self, url: str, device: str = DEVICE):
        self.url = url.rstrip("/")
        self.device = device
        self.http = httpx.Client(headers={"Authorization": f"Bearer {token()}"}, timeout=httpx.Timeout(30, read=120))

    def _check(self, resp: httpx.Response) -> Dict[str, Any]:
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:400]}")
        return resp.json()

    def get(self, path: str, **params: Any) -> Dict[str, Any]:
        return self._check(self.http.get(self.url + path, params=params))

    def post(self, path: str, body: Dict[str, Any], **headers: str) -> Dict[str, Any]:
        return self._check(self.http.post(self.url + path, json=body, headers=headers))

    def message(self, text: str, chat: str = "current", stop: Optional[Callable[[Dict[str, Any]], bool]] = None,
                key: Optional[str] = None) -> List[Dict[str, Any]]:
        """POST a message and read its stream to `reply_end` (or until `stop(event)` drops the stream early,
        as a barge-in does)."""
        body = {"device_id": self.device, "text": text, "origin": {"kind": "sim"}}
        headers = {"Idempotency-Key": key or str(uuid.uuid4()), "Accept": "text/event-stream"}
        events: List[Dict[str, Any]] = []
        with self.http.stream("POST", f"{self.url}/v1/chats/{chat}/messages", json=body, headers=headers) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:400]}")
            for raw in parse_sse(resp.iter_lines()):
                event = raw["data"]
                event["at"] = time.monotonic()
                events.append(event)
                if stop is not None and stop(event):
                    break
                if event["type"] == "reply_end":
                    break
        return events

    def decide(self, chat_id: str, interaction_id: str, decision: str) -> Dict[str, Any]:
        return self.post(f"/v1/chats/{chat_id}/{decision}", {"interaction_id": interaction_id,
                                                             "decision_id": str(uuid.uuid4()), "via": "panel"})

    def answer(self, chat_id: str, interaction_id: str, text: str) -> Dict[str, Any]:
        return self.post(f"/v1/chats/{chat_id}/answer", {"interaction_id": interaction_id,
                                                         "decision_id": str(uuid.uuid4()), "text": text, "via": "panel"})

    def interrupt(self, chat_id: str, reason: str = "panel") -> Dict[str, Any]:
        return self.post(f"/v1/chats/{chat_id}/interrupt", {"decision_id": str(uuid.uuid4()), "reason": reason})

    def new_chat(self) -> Dict[str, Any]:
        return self.post("/v1/chats", {"device_id": self.device}, **{"Idempotency-Key": str(uuid.uuid4())})


class Device(threading.Thread):
    """The gateway and device side of the outbox: speech is accepted, spoken and played; cards are shown.
    While `busy` is set, spoken offers are deferred."""

    def __init__(self, client: Client, log: Callable[[str], None] = print):
        super().__init__(daemon=True)
        self.client = client
        self.log = log
        self.offers: List[Dict[str, Any]] = []
        self.busy = threading.Event()
        self.stopping = threading.Event()
        self.connected = threading.Event()

    def run(self) -> None:
        http = httpx.Client(headers=self.client.http.headers, timeout=httpx.Timeout(10, read=15))
        while not self.stopping.is_set():
            try:
                with http.stream("GET", self.client.url + "/v1/deliveries/stream",
                                 params={"device_id": self.client.device}, headers={"Accept": "text/event-stream"}) as r:
                    self.connected.set()
                    for raw in parse_sse(r.iter_lines()):
                        if self.stopping.is_set():
                            return
                        self.handle(raw["data"])
            except httpx.HTTPError:
                time.sleep(0.5)
        self.connected.clear()

    def handle(self, offer: Dict[str, Any]) -> None:
        offer["at"] = time.monotonic()
        self.offers.append(offer)
        payload = offer.get("payload") or {}
        self.log(f"offer {offer['tier']:<9} {offer['kind']:<6} {offer['slot']} state={payload.get('state')} "
                 f"speak={(offer.get('speak') or '')[:60]!r}")

        def ack(outcome: str, **extra: Any) -> None:
            self.client.post(f"/v1/deliveries/{offer['delivery_id']}/ack",
                             {"lease_id": offer["lease_id"], "outcome": outcome, **extra})

        if offer.get("speak") and offer["tier"] != "SILENT":
            if self.busy.is_set():
                ack("deferred", retry_after_ms=1000)
                self.log("ack deferred (busy)")
                return
            ack("accepted", proactive_id=f"sim-{offer['delivery_id']}", ttl_s=60)
            ack("spoken", proactive_id=f"sim-{offer['delivery_id']}")
            ack("played")
            self.log("ack accepted, spoken, played")
        else:
            ack("shown")

    def stop(self) -> None:
        self.stopping.set()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=os.environ.get("AGENT_URL", "http://agent-service:8010"))
    p.add_argument("--device", default=os.environ.get("SIM_DEVICE", DEVICE))
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask")
    a.add_argument("text")
    a.add_argument("--chat", default="current")
    w = sub.add_parser("watch")
    w.add_argument("--seconds", type=float, default=60)
    w.add_argument("--busy", type=float, default=0, help="defer spoken offers for this many seconds")
    for name in ("task", "trace"):
        sub.add_parser(name).add_argument("task")
    for name in ("approve", "deny"):
        d = sub.add_parser(name)
        d.add_argument("chat")
        d.add_argument("interaction")
    an = sub.add_parser("answer")
    an.add_argument("chat")
    an.add_argument("interaction")
    an.add_argument("text")
    sub.add_parser("interrupt").add_argument("chat")
    ch = sub.add_parser("chats")
    ch.add_argument("action", choices=["list", "new", "switch", "back"])
    ch.add_argument("chat", nargs="?")
    sub.add_parser("status")
    args = p.parse_args()
    c = Client(args.url, args.device)
    if args.cmd == "ask":
        t0 = time.monotonic()
        for event in c.message(args.text, args.chat):
            data = event["data"]
            print(f"{event['at'] - t0:6.2f}s {event['type']:<11} "
                  f"{data.get('delta') if event['type'] == 'text' else json.dumps(data, ensure_ascii=False)[:300]}")
        return
    if args.cmd == "watch":
        device = Device(c)
        device.start()
        if args.busy:
            device.busy.set()
            threading.Timer(args.busy, device.busy.clear).start()
        time.sleep(args.seconds)
        device.stop()
        return
    if args.cmd == "task":
        out: Any = c.get(f"/v1/tasks/{args.task}")
    elif args.cmd == "trace":
        out = c.get(f"/v1/tasks/{args.task}/trace")
    elif args.cmd in ("approve", "deny"):
        out = c.decide(args.chat, args.interaction, args.cmd)
    elif args.cmd == "answer":
        out = c.answer(args.chat, args.interaction, args.text)
    elif args.cmd == "interrupt":
        out = c.interrupt(args.chat)
    elif args.cmd == "chats":
        if args.action == "list":
            out = c.get("/v1/chats", device_id=c.device)
        elif args.action == "new":
            out = c.new_chat()
        elif args.action == "switch":
            out = c.post(f"/v1/chats/{args.chat}/switch", {"device_id": c.device})
        else:
            out = c.post("/v1/chats/back", {"device_id": c.device})
    else:
        out = c.get("/v1/status")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
