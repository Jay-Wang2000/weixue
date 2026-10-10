"""Agent delivery gateway (docs/agent-protocol.md): mirrors the latest `agent` item per task per
device, pushes items to sessions that advertise `features.agent`, batches them into proactive
poll bodies, sends snapshots, resends unacked items, speaks spoken items through the proactive
queue, turns device responses and `played` acks into delivery acks, and answers each device
response with an `agent_ack` receipt.

Sources: fixture injection at POST /weixue/agent/messages (always) and the agent-service outbox
stream when AGENT_SERVICE_URL is set. Acks go to the sink: logged and kept for
GET /weixue/agent/deliveries, and posted to agent-service in live mode."""
import asyncio
import json
import os
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Optional

import weixue_agent_msg as wire
from config.logger import setup_logging

TAG = __name__
SERVICE_URL_ENV = "AGENT_SERVICE_URL"
SERVICE_TOKEN_ENV = "AGENT_SERVICE_TOKEN"
SEQ_HEADER = "X-Weixue-Agent-Seq"
RESPONSE_METHOD = "notifications/weixue/agent_response"
PLAYED_METHOD = "notifications/weixue/played"

RESEND_SECONDS = 3
MAX_SENDS = 4
MIRROR_TASKS = 8
HOLD_AFTER_STOP_SECONDS = 8
HOLD_MAX_SECONDS = 120
LOST_SECONDS = 30
SPEECH_ATTEMPTS = 3
SPEECH_TTL_SECONDS = 600
SPEECH_CLIENT = "weixue-agent"
DEDUP_KEEP = 1024
ACK_VIEW = 500
TICK_SECONDS = 0.5
# The poll body without its `agent` array.
POLL_BASE_BYTES = 96
# Times a snapshot page the device did not apply is sent again before the snapshot moves on.
PAGE_RETRIES = 3
SPEECH_FINAL = ("played", "cut_off", "lost", "dropped")
OPEN_CARD_STATES = ("needs_approval", "needs_input")


@dataclass
class Entry:
    """One wire item queued for a device."""
    payload: dict
    delivery_id: Optional[str] = None
    lease_id: Optional[str] = None
    raw: bool = False
    replied: bool = False
    sends: int = 0
    last_sent: float = 0.0

    @property
    def seq(self):
        return self.payload.get("seq", 0) if isinstance(self.payload.get("seq"), int) else 0


@dataclass
class Device:
    seq: int = 0
    mirror: "OrderedDict[str, Entry]" = field(default_factory=OrderedDict)
    outgoing: list = field(default_factory=list)
    acked_upto: int = 0
    snapshot_due: bool = False
    snapshot_from: int = 0
    polls_with_seq: bool = False
    sessions: list = field(default_factory=list)
    page: Optional["Page"] = None
    page_retries: int = 0


@dataclass
class Page:
    """The snapshot part of the last poll body: where it started and the highest seq in it."""
    start: int
    upto: int


@dataclass
class Speech:
    id: str
    device: str
    delivery_id: str
    lease_id: Optional[str]
    task_id: str
    text: str
    urgent: bool
    ttl: int
    attempt: int = 1
    state: str = "queued"
    proactive_id: Optional[str] = None
    sentence_id: Optional[str] = None
    retry_at: float = 0.0
    started: float = 0.0
    stop_at: Optional[float] = None
    ended_at: Optional[float] = None
    played: Optional[dict] = None
    spoken_acked: bool = False


def _eviction_victim(mirror):
    """The task the device store evicts first (weixue_agent_store.h): the oldest seen terminal
    task, else the oldest terminal task, else the oldest working task, else the oldest task that
    is not an open card. None when every task is an open card."""
    def oldest(pred):
        picks = [(e.seq, t) for t, e in mirror.items() if pred(e.payload)]
        return min(picks)[1] if picks else None

    terminal = lambda p: p.get("state") in wire.TERMINAL
    return (oldest(lambda p: terminal(p) and (p.get("seen") or p.get("state") == "done"))
            or oldest(terminal)
            or oldest(lambda p: p.get("state") in ("working", "interrupted"))
            or oldest(lambda p: p.get("state") not in OPEN_CARD_STATES))


class FixtureSink:
    """Delivery acks and device responses: logged and kept for GET /weixue/agent/deliveries."""

    def __init__(self, logger, wall=time.time):
        self.logger = logger
        self.wall = wall
        self.acks = deque(maxlen=ACK_VIEW)
        self.responses = deque(maxlen=ACK_VIEW)

    def ack(self, delivery_id, lease_id, outcome, device, **detail):
        record = {"delivery_id": delivery_id, "outcome": outcome, "device": device,
                  "received_at": round(self.wall(), 3), **{k: v for k, v in detail.items() if v is not None}}
        self.acks.append(record)
        extra = " ".join(f"{k}={v}" for k, v in detail.items() if v is not None)
        self.logger.bind(tag=TAG).info(f"agent ack {outcome} delivery={delivery_id} device={device} {extra}".rstrip())

    def response(self, device, response):
        self.responses.append({"device": device, "received_at": round(self.wall(), 3), **response})
        self.logger.bind(tag=TAG).info(
            f"agent response {response['kind']} device={device} chat={response.get('chat_id')} "
            f"task={response.get('task_id')} card={response.get('card_id')} option={response.get('option_id')} "
            f"via={response['via']} boot={response['boot']} seq={response['seq']}"
        )


class ServiceSink(FixtureSink):
    """FixtureSink plus the agent-service routes: POST /v1/deliveries/{id}/ack and the chat routes."""

    def __init__(self, logger, base_url, token, session_factory=None, wall=time.time):
        super().__init__(logger, wall)
        self.base = base_url.rstrip("/")
        self.token = token
        self._session_factory = session_factory
        self._session = None

    def _http(self):
        if self._session is None:
            import aiohttp

            factory = self._session_factory or aiohttp.ClientSession
            self._session = factory(headers={"Authorization": f"Bearer {self.token}"})
        return self._session

    async def _post(self, path, body, headers=None):
        try:
            async with self._http().post(self.base + path, json=body, headers=headers) as resp:
                if resp.status >= 400:
                    self.logger.bind(tag=TAG).warning(f"agent-service {path}: HTTP {resp.status}")
                return resp.status
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"agent-service {path}: {type(e).__name__}")
            return 0

    def _spawn(self, coro):
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()

    def ack(self, delivery_id, lease_id, outcome, device, **detail):
        super().ack(delivery_id, lease_id, outcome, device, **detail)
        if lease_id and delivery_id and not delivery_id.startswith("fx_"):
            body = {"lease_id": lease_id, "outcome": outcome}
            for key in ("proactive_id", "ttl_s", "retry_after_ms", "detail"):
                if detail.get(key) is not None:
                    body[key] = detail[key]
            if detail.get("info"):
                body["detail"] = json.dumps(detail["info"], separators=(",", ":"))[:200]
            self._spawn(self._post(f"/v1/deliveries/{delivery_id}/ack", body))

    def response(self, device, response):
        super().response(device, response)
        try:
            route = wire.response_route(device, response)
        except wire.WireError as e:
            self.logger.bind(tag=TAG).warning(f"agent response {response['kind']} device={device} dropped: {e}")
            return
        if route is not None:
            _method, path, body = route
            headers = {"Idempotency-Key": wire.decision_id(device, response["boot"], response["seq"])}
            self._spawn(self._post(path, body, headers))


class AgentGateway:
    def __init__(self, proactive, clock=time.monotonic, wall=time.time, sink=None):
        self.proactive = proactive
        self.clock = clock
        self.wall = wall
        self.logger = setup_logging()
        self.sink = sink or FixtureSink(self.logger, wall)
        self.devices = {}
        self.speeches = OrderedDict()
        self.seen_events = OrderedDict()
        self.card_decisions = OrderedDict()
        self._runner = None
        self._sources = {}
        self.service_url = ""
        self.service_token = ""

    # Device state

    def device(self, device_id):
        return self.devices.setdefault(device_id, Device())

    def _next_seq(self, dev):
        dev.seq = max(dev.seq + 1, int(self.wall() * 1000))
        return dev.seq

    def live_sessions(self, dev):
        dev.sessions = [c for c in dev.sessions if not c.stop_event.is_set()]
        return dev.sessions

    def _log(self, message):
        self.logger.bind(tag=TAG).info(message)

    # Sources

    def submit(self, device_id, fields, *, seq=None, delivery_id=None, lease_id=None, speech=None, raw=False):
        """Queues one item for the device (fixture fields or a mapped delivery). Returns its record."""
        dev = self.device(device_id)
        delivery_id = delivery_id or f"fx_{uuid.uuid4().hex[:24]}"
        if raw:
            entry = Entry(payload=fields, delivery_id=delivery_id, raw=True)
            dev.outgoing.append(entry)
            self._log(f"agent raw item queued device={device_id} bytes={wire.size(fields)}")
            self._dispatch(device_id, dev, [entry])
            return {"delivery_id": delivery_id, "raw": True, "bytes": wire.size(fields)}
        if seq is None:
            seq = self._next_seq(dev)
        else:
            dev.seq = max(dev.seq, seq)
        payload = wire.build_item(fields, seq, int(self.wall() * 1000))
        entry = Entry(payload=payload, delivery_id=delivery_id, lease_id=lease_id)
        task = payload["task_id"]
        latest = dev.mirror.get(task)
        if latest is None and len(dev.mirror) >= MIRROR_TASKS:
            old_task = _eviction_victim(dev.mirror)
            if old_task is None:
                self._log(f"agent mirror full of open cards device={device_id}: task={task} seq={seq} dropped")
                self.sink.ack(delivery_id, lease_id, "dropped", device_id, seq=seq,
                              detail="device task store full of open cards")
                return {"delivery_id": delivery_id, "seq": seq, "task_id": task, "dropped": True}
            del dev.mirror[old_task]
            self._log(f"agent mirror full: {old_task} no longer mirrored for device={device_id}")
        if latest is None or seq > latest.seq:
            dev.mirror[task] = entry
        dev.outgoing.append(entry)
        self._log(
            f"agent item queued device={device_id} chat={payload['chat_id']} task={task} state={payload['state']} "
            f"seq={seq} tier={payload['delivery']} speaks={payload['speaks']} delivery={delivery_id}"
        )
        record = {"delivery_id": delivery_id, "seq": seq, "task_id": task}
        if payload["speaks"] and speech:
            record["speech_id"] = self._speak(device_id, entry, speech)
        self._dispatch(device_id, dev, [entry])
        return record

    def drop(self, device_id, task_ids):
        """Removes tasks from the device's mirror; the next poll or session carries a snapshot."""
        dev = self.device(device_id)
        dropped = [t for t in task_ids if dev.mirror.pop(t, None) is not None]
        dev.outgoing = [e for e in dev.outgoing if e.raw or e.payload.get("task_id") not in task_ids]
        if dropped:
            dev.snapshot_due = True
            dev.snapshot_from = 0
            self._log(f"agent tasks dropped device={device_id}: {dropped}; snapshot due")
            self._dispatch(device_id, dev, [])
        return dropped

    def offer(self, device_id, offer):
        """One agent-service delivery offer (kind `agent`)."""
        if offer.get("kind") != "agent":
            self.sink.ack(offer.get("delivery_id"), offer.get("lease_id"), "dropped", device_id,
                          detail=f"kind {offer.get('kind')} not carried by protocol v1")
            return None
        try:
            fields, speech = wire.from_delivery(offer)
            return self.submit(device_id, fields, delivery_id=offer["delivery_id"],
                               lease_id=offer.get("lease_id"), speech=speech)
        except (wire.WireError, KeyError) as e:
            self.sink.ack(offer.get("delivery_id"), offer.get("lease_id"), "dropped", device_id, detail=str(e))
            return None

    # Push

    def _dispatch(self, device_id, dev, entries):
        sessions = self.live_sessions(dev)
        if sessions:
            payloads = self._snapshot_payloads(dev) if dev.snapshot_due else None
            if payloads is not None:
                dev.snapshot_due = False
                asyncio.get_running_loop().create_task(self._send(device_id, sessions[-1], payloads, "snapshot"))
            elif entries:
                for e in entries:
                    self._mark_sent(e)
                asyncio.get_running_loop().create_task(
                    self._send(device_id, sessions[-1], [e.payload for e in entries], "push"))
        self.proactive._notify()

    def _mark_sent(self, entry):
        entry.sends += 1
        entry.last_sent = self.clock()

    async def _send(self, device_id, conn, payloads, why):
        for payload in payloads:
            try:
                await conn.websocket.send(wire.frame(payload))
                self._log(
                    f"agent {why} device={device_id} kind={payload.get('kind')} task={payload.get('task_id')} "
                    f"state={payload.get('state')} seq={payload.get('seq')}"
                )
            except Exception as e:
                self.logger.bind(tag=TAG).warning(f"agent {why} to {device_id} failed: {type(e).__name__}")
                return

    @staticmethod
    def _replay(entry):
        """The entry's payload without debug_reply once the device answered it, so a rebooted
        loopback build does not answer again."""
        if entry.replied and "debug_reply" in entry.payload:
            return {k: v for k, v in entry.payload.items() if k != "debug_reply"}
        return entry.payload

    def _snapshot_payloads(self, dev, after=0):
        entries = sorted((e for e in dev.mirror.values() if e.seq > after), key=lambda e: e.seq)
        for e in entries:
            self._mark_sent(e)
        return [self._replay(e) for e in entries] + [wire.build_sync(self._next_seq(dev), list(dev.mirror))]

    def on_agent_session(self, device_id, conn):
        """A verified session that advertised features.agent: it gets every mirrored item, then
        agent_sync (the device may have rebooted since its last poll)."""
        dev = self.device(device_id)
        if conn not in dev.sessions:
            dev.sessions.append(conn)
        payloads = self._snapshot_payloads(dev)
        dev.snapshot_due = False
        dev.snapshot_from = 0
        self._log(f"agent session device={device_id} session={getattr(conn, 'session_id', None)}: "
                  f"snapshot of {len(dev.mirror)}")
        for e in [e for e in dev.outgoing if e.raw]:
            dev.outgoing.remove(e)
            payloads.append(e.payload)
        asyncio.get_running_loop().create_task(self._send(device_id, conn, payloads, "snapshot"))

    # Poll

    def on_poll(self, device_id, header):
        """Records the poll's X-Weixue-Agent-Seq; a poll without it (firmware without the agent
        feature) gets no agent items."""
        try:
            seq = int(header) if header is not None else -1
        except (TypeError, ValueError):
            seq = -1
        if seq < 0:
            if device_id in self.devices:
                self.devices[device_id].polls_with_seq = False
            return
        dev = self.device(device_id)
        dev.polls_with_seq = True
        page, dev.page = dev.page, None
        if seq == 0:
            if not dev.snapshot_due or dev.snapshot_from:
                self._log(f"agent poll device={device_id} seq=0: snapshot due")
            dev.snapshot_due = True
            dev.snapshot_from = 0
            dev.page_retries = 0
        elif page is not None and seq < page.upto and dev.page_retries < PAGE_RETRIES:
            dev.page_retries += 1
            dev.snapshot_due = True
            dev.snapshot_from = page.start
            self._log(f"agent poll device={device_id} seq={seq}: snapshot page up to {page.upto} "
                      f"not applied; sending it again ({dev.page_retries}/{PAGE_RETRIES})")
        else:
            dev.page_retries = 0
        self._acked(device_id, dev, seq, "poll")
        dev.acked_upto = seq
        stuck = [e for e in dev.outgoing if not e.raw and e.seq > seq and e.sends >= MAX_SENDS]
        for e in stuck:
            dev.outgoing.remove(e)
            self._log(f"agent seq={e.seq} unapplied after {e.sends} polls device={device_id}: snapshot due")
        if stuck and not dev.snapshot_due:
            dev.snapshot_due = True
            dev.snapshot_from = 0

    def _poll_entries(self, dev):
        return [e for e in dev.outgoing if (e.raw and e.sends == 0) or (not e.raw and e.seq > dev.acked_upto and e.sends < MAX_SENDS)]

    def poll_ready(self, device_id):
        dev = self.devices.get(device_id)
        if dev is None or not dev.polls_with_seq or self.live_sessions(dev):
            return False
        return dev.snapshot_due or bool(self._poll_entries(dev))

    def poll_items(self, device_id, budget=wire.POLL_BODY_BYTES - POLL_BASE_BYTES):
        """The `agent` array of a poll body: at most POLL_BODY_ENTRIES entries within budget bytes
        (a larger single item goes alone); a snapshot continues from page to page and ends with
        agent_sync once every mirrored task was sent. A snapshot page the next poll's header shows
        unapplied is sent again (on_poll)."""
        dev = self.devices.get(device_id)
        if dev is None or not dev.polls_with_seq:
            return []
        snapshot = dev.snapshot_due
        start = dev.snapshot_from
        if snapshot:
            candidates = sorted((e for e in dev.mirror.values() if e.seq > start), key=lambda e: e.seq)
        else:
            candidates = self._poll_entries(dev)
        out, used = [], 2
        for e in candidates:
            n = wire.size(e.payload) + 1
            if out and (len(out) >= wire.POLL_BODY_ENTRIES or used + n > budget):
                break
            out.append(e)
            used += n
        payloads = [self._replay(e) for e in out]
        for e in out:
            self._mark_sent(e)
        if snapshot:
            if len(out) == len(candidates) and len(out) < wire.POLL_BODY_ENTRIES:
                sync = wire.build_sync(self._next_seq(dev), list(dev.mirror))
                if not out or used + wire.size(sync) + 1 <= budget:
                    payloads.append(sync)
                    dev.snapshot_due = False
                    dev.snapshot_from = 0
            if dev.snapshot_due and out:
                dev.snapshot_from = out[-1].seq
            if payloads:
                dev.page = Page(start=start, upto=max(p["seq"] for p in payloads))
        if payloads:
            self._log(
                f"agent poll body device={device_id} items={len(payloads)} "
                f"seqs={[p.get('seq') for p in payloads]} snapshot={any(p.get('kind') == wire.KIND_SYNC for p in payloads)}"
            )
        return payloads

    # Acks and responses

    def _acked(self, device_id, dev, upto, via):
        done = [e for e in dev.outgoing if not e.raw and 0 < e.seq <= upto]
        done += [e for e in dev.outgoing if e.raw and e.sends > 0]
        for e in done:
            dev.outgoing.remove(e)
            if not e.raw:
                self.sink.ack(e.delivery_id, e.lease_id, "shown", device_id, seq=e.seq, via=via)

    def _verified(self, conn):
        ws = getattr(conn, "websocket", None)
        headers = getattr(getattr(ws, "request", None), "headers", None) or getattr(conn, "headers", None)
        return self.proactive.auth.device(headers)

    def on_device_event(self, conn, method, params):
        """Handles one agent_response or played notification. Returns the `agent_ack` receipt to
        send back on conn (agent_response other than shown, its repeats included), else None."""
        device_id = self._verified(conn)
        if device_id is None:
            self.logger.bind(tag=TAG).warning(f"agent event {method} from an unverified session ignored")
            return None
        try:
            event = wire.parse_played(params) if method == PLAYED_METHOD else wire.parse_response(params)
        except wire.WireError as e:
            self.logger.bind(tag=TAG).warning(f"agent event {method} device={device_id} rejected: {e}")
            return None
        conn._weixue_agent_event_at = self.clock()
        key = (device_id, event["boot"], event["seq"])
        if key in self.seen_events:
            self._log(f"agent event duplicate device={device_id} boot={event['boot']} seq={event['seq']}")
            return self.seen_events[key]
        self.seen_events[key] = None
        while len(self.seen_events) > DEDUP_KEEP:
            self.seen_events.popitem(last=False)
        if method == PLAYED_METHOD:
            self._on_played(device_id, event)
            return None
        dev = self.device(device_id)
        if event["kind"] == "shown":
            self._acked(device_id, dev, event["ack_upto"], "shown")
            dev.acked_upto = max(dev.acked_upto, event["ack_upto"])
            return None

        def receipt(result, decided=None):
            ack = wire.build_receipt(event["boot"], event["seq"], event["kind"], result,
                                     card_id=event.get("card_id"), decided=decided)
            self.seen_events[key] = ack
            return ack

        latest = dev.mirror.get(event.get("task_id"))
        if event["kind"] in wire.CARD_KINDS and latest is not None:
            if latest.payload.get("card_id") != event["card_id"]:
                self._log(
                    f"agent response {event['kind']} device={device_id} task={event.get('task_id')} "
                    f"card={event['card_id']} stale (current card={latest.payload.get('card_id')}): dropped"
                )
                return receipt("stale")
        if event["kind"] in wire.CARD_KINDS:
            card = (device_id, event["card_id"])
            first = self.card_decisions.get(card)
            if first is not None:
                self._log(
                    f"agent response {event['kind']} device={device_id} task={event.get('task_id')} "
                    f"card={event['card_id']} seq={event['seq']} duplicate decision (first {first[0]} "
                    f"boot={first[1]} seq={first[2]}): dropped"
                )
                return receipt("duplicate", decided=first[0])
            self.card_decisions[card] = (event["kind"], event["boot"], event["seq"])
            while len(self.card_decisions) > DEDUP_KEEP:
                self.card_decisions.popitem(last=False)
            if latest is not None:
                latest.replied = True
        self.sink.response(device_id, event)
        if event["kind"] == "ack_seen" and latest is not None:
            latest.replied = True
            self.sink.ack(latest.delivery_id, latest.lease_id, "user_ack", device_id, seq=latest.seq)
        return receipt("accepted")

    async def send_receipt(self, conn, receipt):
        """Sends an `agent_ack` on the session the response arrived on."""
        try:
            await conn.websocket.send(wire.frame(receipt))
        except Exception as e:
            self.logger.bind(tag=TAG).warning(
                f"agent receipt {receipt['response']} seq={receipt['seq']} not sent: {type(e).__name__}")
            return
        self._log(f"agent receipt {receipt['response']} boot={receipt['boot']} seq={receipt['seq']} "
                  f"card={receipt.get('card_id')} result={receipt['result']}")

    # Speech

    def _speak(self, device_id, entry, text):
        payload = entry.payload
        speech = Speech(
            id=uuid.uuid4().hex[:16], device=device_id, delivery_id=entry.delivery_id, lease_id=entry.lease_id,
            task_id=payload["task_id"], text=text, urgent=payload["delivery"] == "interrupt", ttl=SPEECH_TTL_SECONDS,
        )
        self.speeches[speech.id] = speech
        while len(self.speeches) > ACK_VIEW:
            self.speeches.popitem(last=False)
        self._submit_speech(speech)
        return speech.id

    def _submit_speech(self, speech):
        from weixue_proactive import RequestError

        fields = {
            "mode": "announce", "text": speech.text[:500], "context": "", "device": speech.device,
            "urgent": speech.urgent, "key": f"{speech.delivery_id}:{speech.attempt}", "ttl": speech.ttl,
            "agent": {"speech": speech.id},
        }
        try:
            status, body = self.proactive.submit(fields, SPEECH_CLIENT)
        except RequestError as e:
            speech.state = "deferred"
            speech.retry_at = self.clock() + int(e.body.get("retry_after", 5))
            self.sink.ack(speech.delivery_id, speech.lease_id, "deferred", speech.device,
                          retry_after_ms=int(e.body.get("retry_after", 5)) * 1000, detail=e.body.get("error"))
            return
        speech.proactive_id = body["id"]
        speech.state = "handed_off"
        speech.started = self.clock()
        self._log(f"agent speech {speech.id} handed off: delivery={speech.delivery_id} proactive={body['id']} "
                  f"urgent={speech.urgent} attempt={speech.attempt} reason={body.get('reason') or '-'}")
        self.sink.ack(speech.delivery_id, speech.lease_id, "accepted", speech.device,
                      proactive_id=body["id"], ttl_s=speech.ttl)

    def speech_for(self, item):
        tag = getattr(item, "agent", None)
        return self.speeches.get(tag.get("speech")) if isinstance(tag, dict) else None

    def _by_sentence(self, sentence_id):
        """The speech whose proactive message speak() played as sentence_id."""
        for speech in reversed(self.speeches.values()):
            if speech.sentence_id is None and speech.proactive_id is not None:
                msg = self.proactive.messages.get(speech.proactive_id)
                tag = getattr(msg, "agent", None) or {}
                speech.sentence_id = tag.get("sentence_id")
            if speech.sentence_id == sentence_id:
                return speech
        return None

    def on_tts(self, sentence_id, state):
        speech = self._by_sentence(sentence_id)
        if speech is None:
            return
        if state == "start":
            self._log(f"agent speech {speech.id} tts start sentence={sentence_id} device={speech.device}")
        elif state == "stop":
            speech.stop_at = self.clock()
            self._log(f"agent speech {speech.id} tts stop sentence={sentence_id}")

    def _on_played(self, device_id, event):
        speech = self._by_sentence(event["sentence_id"])
        if speech is None:
            self._log(f"agent played for an unknown sentence {event['sentence_id']} device={device_id}")
            return
        speech.played = event
        if speech.state in SPEECH_FINAL:
            return
        speech.state = "played" if event["complete"] else "cut_off"
        self._log(f"agent speech {speech.id} {speech.state}: sentence={event['sentence_id']} "
                  f"cut_at_ms={event.get('cut_at_ms')} reason={event.get('reason')}")
        self._spoken(speech)
        self.sink.ack(speech.delivery_id, speech.lease_id, speech.state, device_id, sentence_id=event["sentence_id"],
                      boot=event["boot"], device_seq=event["seq"], cut_at_ms=event.get("cut_at_ms"),
                      reason=event.get("reason"))

    def _spoken(self, speech):
        if not speech.spoken_acked:
            speech.spoken_acked = True
            self.sink.ack(speech.delivery_id, speech.lease_id, "spoken", speech.device, proactive_id=speech.proactive_id)

    async def await_played(self, item, conn):
        """Holds a session after a tagged message was spoken: until its played ack, or
        HOLD_AFTER_STOP_SECONDS after its tts stop, or the session closes."""
        speech = self.speech_for(item)
        if speech is None:
            return
        deadline = self.clock() + HOLD_MAX_SECONDS
        while speech.played is None and not conn.stop_event.is_set() and self.clock() < deadline:
            if speech.stop_at is not None and self.clock() - speech.stop_at >= HOLD_AFTER_STOP_SECONDS:
                break
            await asyncio.sleep(0.1)

    def _track_speech(self):
        now = self.clock()
        for speech in list(self.speeches.values()):
            if speech.state in SPEECH_FINAL:
                continue
            if speech.state == "deferred":
                if now >= speech.retry_at:
                    self._submit_speech(speech)
                continue
            msg = self.proactive.messages.get(speech.proactive_id)
            status = msg.status if msg is not None else None
            if status in ("delivered", "partial"):
                if speech.ended_at is None:
                    speech.ended_at = now
                self._spoken(speech)
            elif status in ("failed", "expired"):
                # Both end before any audio was sent (proactive delivery rules).
                if speech.attempt < SPEECH_ATTEMPTS and status == "failed":
                    speech.attempt += 1
                    speech.sentence_id = None
                    self.sink.ack(speech.delivery_id, speech.lease_id, "deferred", speech.device, detail=msg.error or status)
                    self._submit_speech(speech)
                else:
                    speech.state = "dropped"
                    self.sink.ack(speech.delivery_id, speech.lease_id, "dropped", speech.device, detail=status)
                continue
            elif msg is None and speech.proactive_id is not None:
                speech.state = "lost"
                self.sink.ack(speech.delivery_id, speech.lease_id, "lost", speech.device, detail="proactive record gone")
                continue
            reference = speech.stop_at if speech.stop_at is not None else speech.ended_at
            if speech.played is None and reference is not None and now - reference >= LOST_SECONDS:
                speech.state = "lost"
                self._log(f"agent speech {speech.id} lost: no played ack {LOST_SECONDS}s after playback ended")
                self.sink.ack(speech.delivery_id, speech.lease_id, "lost", speech.device, sentence_id=speech.sentence_id)

    # Resend

    def _resend(self):
        now = self.clock()
        for device_id, dev in self.devices.items():
            sessions = self.live_sessions(dev)
            if not sessions:
                continue
            due = [e for e in dev.outgoing if not e.raw and e.seq > dev.acked_upto and e.sends > 0
                   and now - e.last_sent >= RESEND_SECONDS]
            resend = []
            for e in due:
                if e.sends >= MAX_SENDS:
                    dev.outgoing.remove(e)
                    dev.snapshot_due = True
                    self._log(f"agent seq={e.seq} unacked after {e.sends} sends device={device_id}: snapshot due")
                else:
                    self._mark_sent(e)
                    resend.append(self._replay(e))
            if resend:
                asyncio.get_running_loop().create_task(self._send(device_id, sessions[-1], resend, "resend"))

    async def run(self):
        while True:
            try:
                self._resend()
                self._track_speech()
            except Exception as e:
                self.logger.bind(tag=TAG).error(f"agent gateway tick failed: {type(e).__name__}: {e}")
            await asyncio.sleep(TICK_SECONDS)

    def start(self):
        if self._runner is None or self._runner.done():
            self._runner = asyncio.get_running_loop().create_task(self.run())

    # agent-service outbox stream

    def watch_device(self, device_id):
        """Starts the agent-service stream for a device once (live mode only)."""
        if not self.service_url or device_id in self._sources:
            return
        self._sources[device_id] = asyncio.get_running_loop().create_task(self._stream(device_id))

    async def _stream(self, device_id):
        import aiohttp

        url = f"{self.service_url.rstrip('/')}/v1/deliveries/stream"
        headers = {"Authorization": f"Bearer {self.service_token}", "Accept": "text/event-stream"}
        backoff = 1
        while True:
            try:
                async with aiohttp.ClientSession(headers=headers) as http:
                    async with http.get(url, params={"device_id": device_id}, timeout=None) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"HTTP {resp.status}")
                        backoff = 1
                        async for offer in parse_sse(resp.content):
                            self.offer(device_id, offer)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.bind(tag=TAG).warning(f"agent-service stream for {device_id}: {type(e).__name__}: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    # Views

    def summary(self):
        devices = {}
        for device_id, dev in self.devices.items():
            devices[device_id] = {
                "acked_upto": dev.acked_upto,
                "snapshot_due": dev.snapshot_due,
                "agent_sessions": len(self.live_sessions(dev)),
                "polls_with_seq": dev.polls_with_seq,
                "mirror": [{"task_id": t, "seq": e.seq, "state": e.payload.get("state"), "delivery_id": e.delivery_id}
                           for t, e in dev.mirror.items()],
                "unacked": [{"seq": e.seq, "task_id": e.payload.get("task_id"), "sends": e.sends, "raw": e.raw}
                            for e in dev.outgoing],
            }
        speeches = [
            {"speech_id": s.id, "delivery_id": s.delivery_id, "task_id": s.task_id, "state": s.state,
             "proactive_id": s.proactive_id, "sentence_id": s.sentence_id, "attempt": s.attempt, "played": s.played}
            for s in self.speeches.values()
        ]
        return {"devices": devices, "speeches": speeches, "acks": list(self.sink.acks),
                "responses": list(self.sink.responses)}

    def add_routes(self, app, guarded):
        from aiohttp import web
        from weixue_proactive import RequestError

        async def messages(request):
            try:
                body = await request.json()
            except ValueError:
                raise RequestError(400, "body must be JSON")
            if not isinstance(body, dict):
                raise RequestError(400, "body must be a JSON object")
            device_id = body.get("device")
            if not isinstance(device_id, str) or not device_id.strip():
                raise RequestError(400, "device is required")
            device_id = device_id.strip().lower()
            items = body.get("items") or []
            if not isinstance(items, list):
                raise RequestError(400, "items must be a list")
            results = []
            for item in items:
                if not isinstance(item, dict):
                    raise RequestError(400, "each item must be an object")
                item = dict(item)
                raw = item.pop("raw", None)
                if raw is not None:
                    results.append(self.submit(device_id, raw, raw=True))
                    continue
                seq = item.pop("seq", None)
                speech = item.pop("speech", None)
                try:
                    results.append(self.submit(device_id, item, seq=seq, speech=speech))
                except wire.WireError as e:
                    raise RequestError(400, str(e), accepted=results)
            dropped = self.drop(device_id, body.get("drop") or []) if body.get("drop") else []
            return web.json_response({"items": results, "dropped": dropped}, status=202)

        async def deliveries(_request):
            return web.json_response(self.summary())

        app.add_routes([
            web.post("/weixue/agent/messages", guarded(messages)),
            web.get("/weixue/agent/deliveries", guarded(deliveries)),
        ])


async def parse_sse(stream):
    """Yields the JSON data of `event: delivery` server-sent events from an aiohttp stream."""
    event, data = None, []
    async for raw in stream:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if event == "delivery" and data:
                try:
                    yield json.loads("\n".join(data))
                except ValueError:
                    pass
            event, data = None, []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())


# Hooks into upstream


class _TaggedSocket:
    """A websocket whose `tts` text frames carry sentence_id and ack:true."""

    def __init__(self, ws, sentence_id):
        self._ws = ws
        self._sentence_id = sentence_id

    async def send(self, data):
        if isinstance(data, str) and '"tts"' in data:
            try:
                msg = json.loads(data)
            except ValueError:
                msg = None
            if isinstance(msg, dict) and msg.get("type") == "tts":
                msg["sentence_id"] = self._sentence_id
                msg["ack"] = True
                data = json.dumps(msg)
        return await self._ws.send(data)

    def __getattr__(self, name):
        return getattr(self._ws, name)


class _ConnView:
    """The connection as seen by one send_tts_message call: its websocket is a _TaggedSocket."""

    def __init__(self, conn, socket):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "websocket", socket)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)


def tag_tts_messages(send_tts_message, gateway):
    """Wraps send_tts_message: while the connection's current sentence is the agent sentence
    (conn._weixue_ack_sentence), its tts frames carry sentence_id and ack:true."""

    async def wrapper(conn, state, text=None):
        sentence_id = getattr(conn, "_weixue_ack_sentence", None)
        if sentence_id is None or sentence_id != getattr(conn, "sentence_id", None):
            return await send_tts_message(conn, state, text)
        if state == "start":
            gateway.on_tts(sentence_id, state)
        result = await send_tts_message(_ConnView(conn, _TaggedSocket(conn.websocket, sentence_id)), state, text)
        if state == "stop":
            gateway.on_tts(sentence_id, state)
        return result

    wrapper.weixue_wrapped = send_tts_message
    return wrapper


def route_device_events(handle, gateway):
    """Wraps McpTextMessageHandler.handle: agent_response and played notifications reach the
    gateway, and its receipt goes back on the session, before the wrapped handler logs them."""

    async def wrapper(self, conn, msg_json):
        payload = msg_json.get("payload")
        method = payload.get("method") if isinstance(payload, dict) else None
        if method in (RESPONSE_METHOD, PLAYED_METHOD):
            receipt = gateway.on_device_event(conn, method, payload.get("params"))
            if receipt is not None:
                await gateway.send_receipt(conn, receipt)
        return await handle(self, conn, msg_json)

    wrapper.weixue_agent_wrapped = handle
    return wrapper


def register_agent_sessions(handle, gateway):
    """Wraps HelloTextMessageHandler.handle: a verified session whose hello has features.agent
    is registered with the gateway after the server hello went out."""

    async def wrapper(self, conn, msg_json):
        result = await handle(self, conn, msg_json)
        features = msg_json.get("features") if isinstance(msg_json, dict) else None
        agent = features.get("agent") if isinstance(features, dict) else None
        if isinstance(agent, int) and not isinstance(agent, bool) and agent >= wire.VERSION:
            device_id = gateway._verified(conn)
            if device_id is not None:
                gateway.on_agent_session(device_id, conn)
                gateway.watch_device(device_id)
        return result

    wrapper.weixue_agent_wrapped = handle
    return wrapper


INSTANCE: Optional[AgentGateway] = None


def instance(proactive=None):
    global INSTANCE
    if INSTANCE is None:
        import weixue_proactive

        proactive = proactive or weixue_proactive.instance()
        logger = setup_logging()
        url = os.environ.get(SERVICE_URL_ENV, "")
        token = os.environ.get(SERVICE_TOKEN_ENV, "")
        sink = ServiceSink(logger, url, token) if url else None
        INSTANCE = AgentGateway(proactive, sink=sink)
        INSTANCE.service_url = url
        INSTANCE.service_token = token
        proactive.agent = INSTANCE
    return INSTANCE


def apply():
    """Installs the gateway: proactive hooks, tts tagging, device events and agent sessions."""
    import importlib

    from core.handle.textHandler.helloMessageHandler import HelloTextMessageHandler

    gateway = instance()
    send_audio = importlib.import_module("core.handle.sendAudioHandle")
    if getattr(send_audio.send_tts_message, "weixue_wrapped", None) is None:
        send_audio.send_tts_message = tag_tts_messages(send_audio.send_tts_message, gateway)
    mcp = importlib.import_module("core.handle.textHandler.mcpMessageHandler").McpTextMessageHandler
    if getattr(mcp.handle, "weixue_agent_wrapped", None) is None:
        mcp.handle = route_device_events(mcp.handle, gateway)
    if getattr(HelloTextMessageHandler.handle, "weixue_agent_wrapped", None) is None:
        HelloTextMessageHandler.handle = register_agent_sessions(HelloTextMessageHandler.handle, gateway)
    return gateway
