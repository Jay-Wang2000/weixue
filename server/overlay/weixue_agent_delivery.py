"""Agent delivery gateway (docs/agent-protocol.md): mirrors the latest `agent` item per task per
device and the device's chat list, pushes items to sessions that advertise `features.agent`,
batches them into proactive poll bodies, sends snapshots, resends unacked items, speaks spoken
items through the proactive queue, turns device responses and `played` acks into delivery acks,
and answers each device response with an `agent_ack` receipt.

Sources: fixture injection at POST /weixue/agent/messages (always), and in live mode
(AGENT_SERVICE_URL set) the agent-service device snapshot (restored before the first pruning
agent_sync), outbox stream and chat index (GET /v1/chats). Acks go to the sink: logged and kept
for GET /weixue/agent/deliveries, and in live mode posted to agent-service in order per delivery,
retried until the service answers. Speech plays only after agent-service recorded its hand-off;
card decisions are receipted after agent-service answered them. Hand-offs, card decisions, wire
seqs and unsent acks are kept in a state file across restarts. Open cards are reconciled against
agent-service task state after the restore, on every stream reconnect and before every seq-0 or
session snapshot. A result played whole or tapped is mirrored seen; speech of a card that was
decided or replaced is withdrawn unless it started playing."""
import asyncio
import json
import os
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

import weixue_agent_msg as wire
from config.logger import setup_logging

TAG = __name__
SERVICE_URL_ENV = "AGENT_SERVICE_URL"
SERVICE_TOKEN_ENV = "AGENT_SERVICE_TOKEN"
SEQ_HEADER = "X-Weixue-Agent-Seq"
FEATURE_HEADER = "X-Weixue-Agent-Feature"
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
SWITCH_TIMEOUT_SECONDS = 3
# A card decision is retried with the same decision id for this long before the device is told
# it was not recorded (the device outbox gives a response up about 20 s after its first send).
DECISION_WINDOW_SECONDS = 12
REQUEST_SECONDS = 4
# A spoken item's `accepted` ack is retried for this long; without the service's answer it is not played.
ACCEPT_WINDOW_SECONDS = 8
# Other acks are retried (backoff up to ACK_BACKOFF_MAX_SECONDS) for this long.
ACK_RETRY_SECONDS = 600
ACK_BACKOFF_MAX_SECONDS = 30
# First retry delay of a decision or `accepted` post, and of other acks; each later one doubles.
RETRY_FIRST_SECONDS = 0.5
ACK_RETRY_FIRST_SECONDS = 1
# HTTP answers after which an ack or a decision is not sent again.
FINAL_STATUSES = (400, 401, 403, 404, 409, 422)
DEFERRED_RETRY_MS = 5000
SPEECH_FINAL = ("played", "cut_off", "lost", "dropped", "deferred", "stale", "unaccepted")
# Speech states after which the hand-off was made: the delivery is never spoken again.
HANDED_OFF = ("handed_off", "played", "cut_off", "lost")
OPEN_CARD_STATES = ("needs_approval", "needs_input")
RESULT_STATES = wire.TERMINAL + ("unread",)
STATE_KEEP = 256
# Per GET /v1/tasks/{id} of a reconcile; the requests run concurrently.
RECONCILE_SECONDS = 2
SERVICE_TERMINAL = ("done", "failed", "cancelled")


@dataclass
class Entry:
    """One wire item queued for a device."""
    payload: dict
    delivery_id: Optional[str] = None
    lease_id: Optional[str] = None
    # agent-service gseq of the delivery (live mode), the order of restored and offered cards.
    gseq: Optional[int] = None
    raw: bool = False
    sends: int = 0
    last_sent: float = 0.0
    # The seq the entry was queued with: its age when a boot snapshot gives it a new seq.
    born: int = 0

    def __post_init__(self):
        self.born = self.born or self.seq

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
    # features.agent of the newest session or X-Weixue-Agent-Feature of the last poll; 0 = unknown.
    level: int = 0
    listing: Optional[dict] = None
    listing_gen: int = 0
    listing_delivery: tuple = (None, None)
    chats: Optional[Entry] = None
    chats_key: Optional[tuple] = None
    # The agent-service device snapshot was restored into the mirror (fixture mode: always).
    restored: bool = True
    # A seq-0 poll asked for a snapshot whose cards have not been ranked yet.
    boot_snapshot: bool = False
    # The reconcile the next snapshot waits for.
    reconciling: Optional[asyncio.Task] = None


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
    # queued (fixture) | accepting | handed_off | SPEECH_FINAL
    state: str = "queued"
    proactive_id: Optional[str] = None
    sentence_id: Optional[str] = None
    started: float = 0.0
    stop_at: Optional[float] = None
    ended_at: Optional[float] = None
    played: Optional[dict] = None
    spoken_acked: bool = False
    # Wire state of the spoken item, and its card_id when it is an open card.
    item_state: Optional[str] = None
    card_id: Optional[str] = None


def _ended(latest, payload):
    """Whether payload would reopen a task whose mirrored agent-service item is terminal."""
    if latest is None or latest.delivery_id is None or latest.delivery_id.startswith("fx_"):
        return False
    done = lambda state: state in wire.TERMINAL or state == "unread"
    return done(latest.payload.get("state")) and not done(payload.get("state"))


def _card_replaced(latest, payload):
    """Whether payload takes the place of latest's open card (another state or another card)."""
    return latest.get("state") in OPEN_CARD_STATES and (
        payload.get("state") != latest.get("state") or payload.get("card_id") != latest.get("card_id"))


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

    live = False

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

    def connection(self, device, kind, session_id=None):
        self.logger.bind(tag=TAG).info(f"agent connection {kind} device={device} session={session_id}")

    def response(self, device, response):
        self.responses.append({"device": device, "received_at": round(self.wall(), 3), **response})
        self.logger.bind(tag=TAG).info(
            f"agent response {response['kind']} device={device} chat={response.get('chat_id')} "
            f"task={response.get('task_id')} card={response.get('card_id')} option={response.get('option_id')} "
            f"via={response['via']} boot={response['boot']} seq={response['seq']}"
        )


def _ack_body(lease_id, outcome, detail):
    body = {"lease_id": lease_id, "outcome": outcome}
    for key in ("proactive_id", "ttl_s", "retry_after_ms", "detail", "sentence_id", "boot", "device_seq",
                "cut_at_ms", "reason"):
        if detail.get(key) is not None:
            body[key] = detail[key]
    if detail.get("info"):
        body["detail"] = json.dumps(detail["info"], separators=(",", ":"))[:200]
    return body


class ServiceSink(FixtureSink):
    """FixtureSink plus the agent-service routes. Acks are posted in order per delivery, each
    retried until the service answers (pending_acks; on_change runs after every change so the
    gateway can keep them). accept() posts a spoken item's `accepted` ack and returns once the
    service recorded it; a card decision's post (response) is retried with its decision id until
    the service answers. A device's switch_chat / new_chat post in flight is pending_switch(device)
    until it returns."""

    live = True

    def __init__(self, logger, base_url, token, session_factory=None, wall=time.time, clock=time.monotonic):
        super().__init__(logger, wall)
        self.base = base_url.rstrip("/")
        self.token = token
        self.clock = clock
        self._session_factory = session_factory
        self._session = None
        self.switches = {}
        self.pending_acks = OrderedDict()
        self._ack_workers = {}
        self.on_change = None

    def pending_switch(self, device):
        return self.switches.get(device)

    async def _switch(self, device, done, post):
        try:
            return await asyncio.wait_for(post, SWITCH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self.logger.bind(tag=TAG).warning(f"agent-service chat switch: no answer within {SWITCH_TIMEOUT_SECONDS} s")
            return 0
        finally:
            if not done.done():
                done.set_result(None)
            if self.switches.get(device) is done:
                del self.switches[device]

    def _http(self):
        if self._session is None:
            import aiohttp

            factory = self._session_factory or aiohttp.ClientSession
            self._session = factory(headers={"Authorization": f"Bearer {self.token}"})
        return self._session

    async def _post(self, path, body, headers=None, timeout=None):
        async def send():
            async with self._http().post(self.base + path, json=body, headers=headers) as resp:
                if resp.status >= 400:
                    self.logger.bind(tag=TAG).warning(f"agent-service {path}: HTTP {resp.status}")
                return resp.status

        try:
            return await (asyncio.wait_for(send(), timeout) if timeout else send())
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"agent-service {path}: {type(e).__name__}")
            return 0

    async def _post_until(self, path, body, headers, window, first_delay=None, whole_window=False):
        """Posts body until a 2xx or a FINAL_STATUSES answer, or window seconds passed; returns the
        last status (0: no answer). With whole_window a request may take the rest of the window, so
        a slow request is never sent a second time while it runs."""
        deadline = self.clock() + window
        delay = RETRY_FIRST_SECONDS if first_delay is None else first_delay
        while True:
            timeout = max(deadline - self.clock(), 0.1) if whole_window else REQUEST_SECONDS
            status = await self._post(path, body, headers, timeout)
            if 200 <= status < 300 or status in FINAL_STATUSES:
                return status
            if self.clock() + delay >= deadline:
                return status
            await asyncio.sleep(delay)
            delay = min(delay * 2, ACK_BACKOFF_MAX_SECONDS)

    async def _get(self, path, params=None):
        """The JSON object of a GET answered 200 within REQUEST_SECONDS, else None."""
        async def fetch():
            async with self._http().get(self.base + path, params=params) as resp:
                if resp.status != 200:
                    self.logger.bind(tag=TAG).warning(f"agent-service {path}: HTTP {resp.status}")
                    return None
                body = await resp.json()
                return body if isinstance(body, dict) else None

        try:
            return await asyncio.wait_for(fetch(), REQUEST_SECONDS)
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"agent-service {path}: {type(e).__name__}")
            return None

    async def get_task(self, task_id):
        """("found", body) of GET /v1/tasks/{id}, ("unknown", None) on 404, None without an answer."""
        async def fetch():
            async with self._http().get(f"{self.base}/v1/tasks/{quote(task_id, safe='')}") as resp:
                if resp.status == 404:
                    return "unknown", None
                if resp.status != 200:
                    return None
                body = await resp.json()
                return ("found", body) if isinstance(body, dict) else None

        try:
            return await asyncio.wait_for(fetch(), RECONCILE_SECONDS)
        except Exception:
            return None

    async def get_chats(self, device):
        """The agent-service chat index of the device (GET /v1/chats), or None."""
        return await self._get("/v1/chats", {"device_id": device})

    async def get_snapshot(self, device):
        """The agent-service device snapshot (GET /v1/devices/{id}/snapshot: cards and chats), or None."""
        return await self._get(f"/v1/devices/{quote(device, safe='')}/snapshot")

    def _spawn(self, coro):
        try:
            return asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return None

    def _changed(self):
        if self.on_change is not None:
            self.on_change()

    def ack(self, delivery_id, lease_id, outcome, device, **detail):
        super().ack(delivery_id, lease_id, outcome, device, **detail)
        if lease_id and delivery_id and not delivery_id.startswith("fx_"):
            self.pending_acks.setdefault(delivery_id, []).append(_ack_body(lease_id, outcome, detail))
            self._changed()
            self._drain(delivery_id)

    def resume(self):
        """Starts posting the acks kept from before a restart."""
        for delivery_id in list(self.pending_acks):
            self._drain(delivery_id)

    def _drain(self, delivery_id):
        worker = self._ack_workers.get(delivery_id)
        if worker is None or worker.done():
            task = self._spawn(self._post_acks(delivery_id))
            if task is not None:
                self._ack_workers[delivery_id] = task

    async def _post_acks(self, delivery_id):
        path = f"/v1/deliveries/{delivery_id}/ack"
        queue = self.pending_acks.get(delivery_id) or []
        while queue:
            body = queue[0]
            status = await self._post_until(path, body, None, ACK_RETRY_SECONDS, ACK_RETRY_FIRST_SECONDS)
            if not (200 <= status < 300):
                self.logger.bind(tag=TAG).warning(
                    f"agent ack {body['outcome']} delivery={delivery_id} not recorded by agent-service (HTTP {status})")
            queue.pop(0)
            self._changed()
        if self.pending_acks.get(delivery_id) is queue and not queue:
            del self.pending_acks[delivery_id]
            self._changed()
        self._ack_workers.pop(delivery_id, None)

    async def accept(self, delivery_id, lease_id, device, **detail):
        """Posts a spoken item's `accepted` ack; "accepted" once the service recorded it, "stale"
        when it refused the lease, "unreachable" without an answer within ACCEPT_WINDOW_SECONDS."""
        status = await self._post_until(f"/v1/deliveries/{delivery_id}/ack",
                                        _ack_body(lease_id, "accepted", detail), None, ACCEPT_WINDOW_SECONDS)
        if 200 <= status < 300:
            FixtureSink.ack(self, delivery_id, lease_id, "accepted", device, **detail)
            return "accepted"
        return "stale" if status in FINAL_STATUSES else "unreachable"

    def connection(self, device, kind, session_id=None):
        super().connection(device, kind, session_id)
        body = {"kind": kind}
        if session_id:
            body["gateway_session_id"] = str(session_id)[:128]
        self._spawn(self._post(f"/v1/devices/{quote(device, safe='')}/connection", body))

    async def _decide(self, path, body, headers):
        status = await self._post_until(path, body, headers, DECISION_WINDOW_SECONDS, whole_window=True)
        if 200 <= status < 300:
            return "accepted"
        return "rejected" if status in FINAL_STATUSES else "unconfirmed"

    def response(self, device, response):
        """Posts the response's route. Returns the task of an approve / deny / answer post (its
        result: accepted, rejected or unconfirmed) or of a switch_chat / new_chat post (its result:
        the HTTP status, 0 without an answer), else None."""
        super().response(device, response)
        try:
            route = wire.response_route(device, response)
        except wire.WireError as e:
            self.logger.bind(tag=TAG).warning(f"agent response {response['kind']} device={device} dropped: {e}")
            return None
        if route is None:
            return None
        _method, path, body = route
        headers = {"Idempotency-Key": body.get("decision_id") or wire.decision_id(device, response["boot"], response["seq"])}
        if response["kind"] in wire.CARD_KINDS:
            return self._spawn(self._decide(path, body, headers))
        post = self._post(path, body, headers)
        if response["kind"] not in wire.SWITCH_KINDS:
            self._spawn(post)
            return None
        try:
            done = asyncio.get_running_loop().create_future()
        except RuntimeError:
            post.close()
            return None
        self.switches[device] = done
        return self._spawn(self._switch(device, done, post))


class AgentGateway:
    def __init__(self, proactive, clock=time.monotonic, wall=time.time, sink=None, state=None):
        from weixue_proactive import StateFile

        self.proactive = proactive
        self.clock = clock
        self.wall = wall
        self.logger = setup_logging()
        self.sink = sink or FixtureSink(self.logger, wall)
        self.devices = {}
        self.speeches = OrderedDict()
        self.seen_events = OrderedDict()
        self.card_decisions = OrderedDict()
        self.decisions_in_flight = {}
        # delivery id → {"attempt", "state"} of its newest spoken attempt.
        self.handoffs = OrderedDict()
        # delivery id → wire seq of its item, so a restored card keeps the seq the device applied.
        self.wire_seqs = OrderedDict()
        self._runner = None
        self._sources = {}
        self.service_url = ""
        self.service_token = ""
        # A reconcile found agent-service unreachable and logged it.
        self._reconcile_failing = False
        self.state = state or StateFile(None)
        self._load()

    # State file

    def _load(self):
        doc = self.state.load()
        for card, first in doc.get("card_decisions") or []:
            self.card_decisions[tuple(card)] = tuple(first)
        for delivery_id, record in doc.get("handoffs") or []:
            self.handoffs[delivery_id] = record
        for delivery_id, seq in doc.get("wire_seqs") or []:
            self.wire_seqs[delivery_id] = seq
        acks = doc.get("acks") or []
        if acks and hasattr(self.sink, "pending_acks"):
            for delivery_id, bodies in acks:
                self.sink.pending_acks[delivery_id] = list(bodies)
        if hasattr(self.sink, "on_change"):
            self.sink.on_change = self._save

    def _save(self):
        """Writes the state file; returns whether it is on disk."""
        def tail(d, n=STATE_KEEP):
            items = list(d.items())
            return [[list(k) if isinstance(k, tuple) else k, list(v) if isinstance(v, tuple) else v]
                    for k, v in items[-n:]]

        for d in (self.handoffs, self.wire_seqs, self.card_decisions):
            while len(d) > STATE_KEEP:
                d.popitem(last=False)
        return self.state.save({
            "card_decisions": tail(self.card_decisions),
            "handoffs": tail(self.handoffs),
            "wire_seqs": tail(self.wire_seqs),
            "acks": [[k, list(v)] for k, v in getattr(self.sink, "pending_acks", {}).items()],
        })

    # Device state

    def device(self, device_id):
        dev = self.devices.get(device_id)
        if dev is None:
            dev = self.devices[device_id] = Device(restored=not (self.service_url and self.sink.live))
        return dev

    def _next_seq(self, dev):
        dev.seq = max(dev.seq + 1, int(self.wall() * 1000))
        return dev.seq

    def live_sessions(self, dev):
        dev.sessions = [c for c in dev.sessions if not c.stop_event.is_set()]
        return dev.sessions

    def _log(self, message):
        self.logger.bind(tag=TAG).info(message)

    # Sources

    def submit(self, device_id, fields, *, seq=None, delivery_id=None, lease_id=None, speech=None, raw=False,
               gseq=None, attempt=1):
        """Queues one item for the device (fixture fields or a mapped delivery). Returns its record.
        A delivery older (gseq) than the task's mirrored card is not sent; its speech still plays."""
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
        entry = Entry(payload=payload, delivery_id=delivery_id, lease_id=lease_id, gseq=gseq)
        task = payload["task_id"]
        latest = dev.mirror.get(task)
        if latest is not None and latest.gseq is not None and gseq is not None and gseq < latest.gseq:
            self._log(f"agent delivery {delivery_id} gseq={gseq} older than the card of task={task} "
                      f"(gseq={latest.gseq}): not sent")
            record = {"delivery_id": delivery_id, "seq": seq, "task_id": task, "superseded": True}
            if payload["speaks"] and speech:
                record["speech_id"] = self._speak(device_id, entry, speech, attempt)
            elif lease_id:
                self.sink.ack(delivery_id, lease_id, "dropped", device_id, detail="older than the task's card")
            return record
        if _ended(latest, payload) and not delivery_id.startswith("fx_"):
            self._log(f"agent delivery {delivery_id} state={payload['state']} for task={task} that ended "
                      f"({latest.payload.get('state')}): not sent")
            if lease_id:
                self.sink.ack(delivery_id, lease_id, "dropped", device_id, detail="task already ended")
            return {"delivery_id": delivery_id, "seq": seq, "task_id": task, "ended": True}
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
            if latest is not None and _card_replaced(latest.payload, payload):
                self._withdraw_card_speech(device_id, latest.payload["card_id"], f"card replaced by {payload['state']}")
            dev.mirror[task] = entry
        dev.outgoing.append(entry)
        if not delivery_id.startswith("fx_"):
            self.wire_seqs[delivery_id] = seq
            self._save()
        self._log(
            f"agent item queued device={device_id} chat={payload['chat_id']} task={task} state={payload['state']} "
            f"seq={seq} tier={payload['delivery']} speaks={payload['speaks']} delivery={delivery_id}"
        )
        record = {"delivery_id": delivery_id, "seq": seq, "task_id": task}
        if payload["speaks"] and speech:
            record["speech_id"] = self._speak(device_id, entry, speech, attempt)
        self._dispatch(device_id, dev, [entry])
        return record

    def drop(self, device_id, task_ids):
        """Removes tasks from the device's mirror; the next poll or session carries a snapshot."""
        dev = self.device(device_id)
        dropped = []
        for t in task_ids:
            entry = dev.mirror.pop(t, None)
            if entry is not None:
                dropped.append(t)
                if entry.payload.get("state") in OPEN_CARD_STATES:
                    self._withdraw_card_speech(device_id, entry.payload.get("card_id"), "task dropped")
        dev.outgoing = [e for e in dev.outgoing if e.raw or e.payload.get("task_id") not in task_ids]
        if dropped:
            dev.snapshot_due = True
            dev.snapshot_from = 0
            self._log(f"agent tasks dropped device={device_id}: {dropped}; snapshot due")
            self._dispatch(device_id, dev, [])
        return dropped

    def offer(self, device_id, offer):
        """One agent-service delivery offer (kind `agent` or `chats`)."""
        if offer.get("kind") == "chats":
            return self.set_chats(device_id, offer.get("payload") or {}, offer.get("delivery_id"), offer.get("lease_id"))
        if offer.get("kind") != "agent":
            self.sink.ack(offer.get("delivery_id"), offer.get("lease_id"), "dropped", device_id,
                          detail=f"kind {offer.get('kind')} not carried by protocol v1")
            return None
        try:
            fields, speech = wire.from_delivery(offer)
            attempt = offer.get("attempt") if isinstance(offer.get("attempt"), int) else 1
            gseq = offer.get("gseq") if isinstance(offer.get("gseq"), int) else None
            return self.submit(device_id, fields, delivery_id=offer["delivery_id"],
                               lease_id=offer.get("lease_id"), speech=speech, gseq=gseq, attempt=max(1, attempt))
        except (wire.WireError, KeyError) as e:
            self.sink.ack(offer.get("delivery_id"), offer.get("lease_id"), "dropped", device_id, detail=str(e))
            return None

    # Chats

    def set_chats(self, device_id, listing, delivery_id=None, lease_id=None):
        """The device's chat index (an agent-service `GET /v1/chats` body): queued as `agent_chats`
        for a device that applies it, unless it lists what the device was last sent."""
        dev = self.device(device_id)
        dev.listing = listing
        dev.listing_gen += 1
        dev.listing_delivery = (delivery_id, lease_id)
        if 0 < dev.level < wire.FEATURE_CHATS:
            if delivery_id:
                self.sink.ack(delivery_id, lease_id, "dropped", device_id, detail="device without agent_chats (v1.1)")
            return None
        if dev.level == 0:
            return None
        return self._queue_chats(device_id, dev)

    def _task_titles(self, dev):
        """Chat id → title (else summary) of its newest mirrored task."""
        titles = {}
        for e in sorted(dev.mirror.values(), key=lambda e: e.seq):
            p = e.payload
            if p.get("title") or p.get("summary"):
                titles[p.get("chat_id")] = p.get("title") or p.get("summary")
        return titles

    def _queue_chats(self, device_id, dev, dispatch=True):
        delivery_id, lease_id = dev.listing_delivery
        dev.listing_delivery = (None, None)
        try:
            current, chats = wire.chats_from_listing(dev.listing, self.wall(), self._task_titles(dev))
            payload = wire.build_chats(self._next_seq(dev), int(self.wall() * 1000), current, chats)
        except wire.WireError as e:
            self.logger.bind(tag=TAG).warning(f"agent chats for {device_id} not sent: {e}")
            if delivery_id:
                self.sink.ack(delivery_id, lease_id, "dropped", device_id, detail=str(e))
            return None
        key = (payload.get("current"), tuple((c["id"], c.get("title"), c["state"]) for c in payload["chats"]))
        if key == dev.chats_key:
            if delivery_id:
                self.sink.ack(delivery_id, lease_id, "shown", device_id, detail="chat list unchanged")
            return None
        dev.outgoing = [e for e in dev.outgoing if e is not dev.chats]
        entry = Entry(payload=payload, delivery_id=delivery_id or f"gw_{uuid.uuid4().hex[:24]}", lease_id=lease_id)
        dev.chats, dev.chats_key = entry, key
        dev.outgoing.append(entry)
        self._log(f"agent chats queued device={device_id} current={payload.get('current')} "
                  f"chats={len(payload['chats'])}/{len(chats)} seq={payload['seq']} bytes={wire.size(payload)} "
                  f"delivery={entry.delivery_id}")
        if dispatch:
            self._dispatch(device_id, dev, [entry])
        return {"delivery_id": entry.delivery_id, "seq": payload["seq"], "chats": len(payload["chats"])}

    def _set_level(self, device_id, dev, level, dispatch=True):
        """Records the device's agent feature level; a device that newly applies agent_chats gets
        the chat list."""
        if level == dev.level:
            return
        dev.level = level
        if level >= wire.FEATURE_CHATS:
            if dev.listing is not None and dev.chats is None:
                self._queue_chats(device_id, dev, dispatch)
        else:
            dev.outgoing = [e for e in dev.outgoing if e is not dev.chats]
            dev.chats, dev.chats_key = None, None
            delivery_id, lease_id = dev.listing_delivery
            dev.listing_delivery = (None, None)
            if delivery_id:
                self.sink.ack(delivery_id, lease_id, "dropped", device_id, detail="device without agent_chats (v1.1)")

    def refresh_chats(self, device_id):
        """Fetches the device's chat index from agent-service (live mode) and queues it, unless a
        newer index arrived while the request ran."""
        fetch = getattr(self.sink, "get_chats", None)
        if fetch is None:
            return None
        dev = self.device(device_id)
        gen = dev.listing_gen

        async def run():
            listing = await fetch(device_id)
            if listing is not None and dev.listing_gen == gen:
                self.set_chats(device_id, listing)

        return asyncio.get_running_loop().create_task(run())

    # Push

    def _dispatch(self, device_id, dev, entries):
        sessions = self.live_sessions(dev)
        if sessions:
            payloads = self._snapshot_payloads(device_id, dev) if self._snapshot_ready(dev) else None
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
    def _snapshot_entries(dev, after=0):
        """Mirrored tasks and the chat list above seq after, oldest first."""
        entries = [e for e in dev.mirror.values() if e.seq > after]
        if dev.chats is not None and dev.chats.seq > after:
            entries.append(dev.chats)
        return sorted(entries, key=lambda e: e.seq)

    def _snapshot_payloads(self, device_id, dev, after=0):
        if after == 0:
            self._rank_boot_snapshot(device_id, dev)
        entries = self._snapshot_entries(dev, after)
        for e in entries:
            self._mark_sent(e)
        return [e.payload for e in entries] + [wire.build_sync(self._next_seq(dev), list(dev.mirror))]

    def on_connection(self, device_id, kind, conn):
        """A verified WebSocket session of the device opened or closed."""
        connection = getattr(self.sink, "connection", None)
        if connection is not None:
            connection(device_id, kind, getattr(conn, "session_id", None))

    def on_agent_session(self, device_id, conn, level=wire.VERSION):
        """A verified session that advertised features.agent (level): it gets every mirrored item
        and the chat list, then agent_sync (the device may have rebooted since its last poll)."""
        dev = self.device(device_id)
        if conn not in dev.sessions:
            dev.sessions.append(conn)
        self._set_level(device_id, dev, level, dispatch=False)
        if self._reconcile_first(device_id, dev):
            payloads = []
            dev.snapshot_due = True
            self._log(f"agent session device={device_id} session={getattr(conn, 'session_id', None)}: "
                      f"snapshot once the open cards are reconciled")
        elif dev.restored:
            payloads = self._snapshot_payloads(device_id, dev)
            dev.snapshot_due = False
            self._log(f"agent session device={device_id} session={getattr(conn, 'session_id', None)}: "
                      f"snapshot of {len(dev.mirror)}")
        else:
            payloads = []
            dev.snapshot_due = True
            self._log(f"agent session device={device_id} session={getattr(conn, 'session_id', None)}: "
                      f"snapshot once the agent-service snapshot is restored")
        dev.snapshot_from = 0
        for e in [e for e in dev.outgoing if e.raw]:
            dev.outgoing.remove(e)
            payloads.append(e.payload)
        asyncio.get_running_loop().create_task(self._send(device_id, conn, payloads, "snapshot"))

    # Poll

    def on_poll(self, device_id, header, feature=None):
        """Records the poll's X-Weixue-Agent-Seq and X-Weixue-Agent-Feature (absent: level 1); a
        poll without the seq (firmware without the agent feature) gets no agent items."""
        try:
            seq = int(header) if header is not None else -1
        except (TypeError, ValueError):
            seq = -1
        if seq < 0:
            if device_id in self.devices:
                self.devices[device_id].polls_with_seq = False
            return
        try:
            level = max(wire.VERSION, int(feature)) if feature is not None else wire.VERSION
        except (TypeError, ValueError):
            level = wire.VERSION
        dev = self.device(device_id)
        dev.polls_with_seq = True
        self._set_level(device_id, dev, level)
        self.watch_device(device_id)
        page, dev.page = dev.page, None
        if seq == 0:
            if not dev.snapshot_due or dev.snapshot_from:
                self._log(f"agent poll device={device_id} seq=0: snapshot due")
            dev.snapshot_due = True
            dev.snapshot_from = 0
            dev.page_retries = 0
            dev.boot_snapshot = True
            self._reconcile_first(device_id, dev)
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

    def _rank_boot_snapshot(self, device_id, dev):
        """Once per seq-0 poll, before its snapshot: gives the mirrored tasks new seqs with open
        approval cards first, then question cards, then the rest, each in the order they were queued."""
        if not dev.boot_snapshot:
            return
        dev.boot_snapshot = False

        def rank(e):
            state = e.payload.get("state")
            kind = OPEN_CARD_STATES.index(state) if state in OPEN_CARD_STATES else len(OPEN_CARD_STATES)
            return kind, e.born

        entries = sorted(dev.mirror.values(), key=rank)
        if [e.seq for e in entries] == sorted(e.seq for e in entries):
            return
        for e in entries:
            e.payload["seq"] = self._next_seq(dev)
            if e.delivery_id and not e.delivery_id.startswith("fx_"):
                self.wire_seqs[e.delivery_id] = e.seq
        self._save()
        self._log(f"agent boot snapshot device={device_id}: cards first, "
                  f"order {[(e.payload.get('task_id'), e.seq) for e in entries]}")

    def _poll_entries(self, dev):
        return [e for e in dev.outgoing if (e.raw and e.sends == 0) or (not e.raw and e.seq > dev.acked_upto and e.sends < MAX_SENDS)]

    def poll_ready(self, device_id):
        dev = self.devices.get(device_id)
        if dev is None or not dev.polls_with_seq or self.live_sessions(dev):
            return False
        if dev.reconciling is not None and not dev.reconciling.done():
            return False
        return self._snapshot_ready(dev) or bool(self._poll_entries(dev))

    def poll_items(self, device_id, budget=wire.POLL_BODY_BYTES - POLL_BASE_BYTES):
        """The `agent` array of a poll body: at most POLL_BODY_ENTRIES entries within budget bytes
        (a larger single item goes alone); a snapshot continues from page to page and ends with
        agent_sync once every mirrored task was sent. A snapshot page the next poll's header shows
        unapplied is sent again (on_poll)."""
        dev = self.devices.get(device_id)
        if dev is None or not dev.polls_with_seq or (dev.reconciling is not None and not dev.reconciling.done()):
            return []
        snapshot = self._snapshot_ready(dev)
        start = dev.snapshot_from
        if snapshot and start == 0:
            self._rank_boot_snapshot(device_id, dev)
        if snapshot:
            candidates = self._snapshot_entries(dev, start)
        else:
            candidates = self._poll_entries(dev)
        out, used = [], 2
        for e in candidates:
            n = wire.size(e.payload) + 1
            if out and (len(out) >= wire.POLL_BODY_ENTRIES or used + n > budget):
                break
            out.append(e)
            used += n
        payloads = [e.payload for e in out]
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
            if card in self.decisions_in_flight:
                del self.seen_events[key]
                self._log(f"agent response {event['kind']} device={device_id} card={event['card_id']} seq={event['seq']}: "
                          f"a decision for the card is being forwarded; no receipt yet")
                return None
            if not self.sink.live:
                self._record_decision(device_id, card, event)
        forward = self.sink.response(device_id, event)
        if event["kind"] == "ack_seen" and latest is not None:
            if latest.payload.get("state") in RESULT_STATES:
                latest.payload["seen"] = True
            self.sink.ack(latest.delivery_id, latest.lease_id, "user_ack", device_id, seq=latest.seq)
        if forward is not None and event["kind"] in wire.CARD_KINDS:
            self.decisions_in_flight[card] = (event["kind"], event["boot"], event["seq"])
            asyncio.get_running_loop().create_task(self._finish_decision(conn, device_id, event, card, forward, receipt))
            return None
        if forward is not None:
            asyncio.get_running_loop().create_task(self._finish_switch(conn, device_id, event, forward, receipt))
            return None
        if event["kind"] in wire.CARD_KINDS and self.sink.live:
            return receipt("stale")
        return receipt("accepted")

    def _record_decision(self, device_id, card, event):
        self.card_decisions[card] = (event["kind"], event["boot"], event["seq"])
        while len(self.card_decisions) > DEDUP_KEEP:
            self.card_decisions.popitem(last=False)
        self._save()
        self._withdraw_card_speech(device_id, event["card_id"], f"card decided ({event['kind']})")

    async def _finish_decision(self, conn, device_id, event, card, forward, receipt):
        """Receipt of an approve / deny / answer once agent-service answered: accepted when it
        recorded the decision (the card's first decision from then on), else stale (the device
        keeps the card open for another tap)."""
        try:
            result = await forward
        except Exception:
            result = "unconfirmed"
        finally:
            self.decisions_in_flight.pop(card, None)
        if result == "accepted":
            self._record_decision(device_id, card, event)
        ack = receipt("accepted" if result == "accepted" else "stale")
        self._log(f"agent {event['kind']} device={device_id} card={event['card_id']} seq={event['seq']} "
                  f"service={result} result={ack['result']}")
        await self.send_receipt(conn, ack)

    async def _finish_switch(self, conn, device_id, event, post, receipt):
        """Receipt of a switch_chat / new_chat once agent-service answered (accepted on 2xx, else
        stale), then the refreshed chat list."""
        try:
            status = await post
        except Exception:
            status = 0
        result = "accepted" if 200 <= (status or 0) < 300 else "stale"
        self._log(f"agent {event['kind']} device={device_id} chat={event.get('chat_id')} service={status} "
                  f"result={result}")
        await self.send_receipt(conn, receipt(result))
        refresh = self.refresh_chats(device_id)
        if refresh is not None:
            await refresh

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

    def _speak(self, device_id, entry, text, attempt=1):
        """Hands a spoken item to the proactive queue. In live mode agent-service records the
        hand-off (`accepted`) first; without that answer the attempt is not played. A delivery with an
        attempt handed off before (handoffs, kept across restarts) is never spoken again, in any
        attempt; an attempt that ended deferred or dropped before audio leaves no hand-off."""
        payload = entry.payload
        speech = Speech(
            id=uuid.uuid4().hex[:16], device=device_id, delivery_id=entry.delivery_id, lease_id=entry.lease_id,
            task_id=payload["task_id"], text=text, urgent=payload["delivery"] == "interrupt", ttl=SPEECH_TTL_SECONDS,
            attempt=attempt, item_state=payload["state"],
            card_id=payload.get("card_id") if payload["state"] in OPEN_CARD_STATES else None,
        )
        self.speeches[speech.id] = speech
        while len(self.speeches) > ACK_VIEW:
            self.speeches.popitem(last=False)
        prior = self.handoffs.get(speech.delivery_id)
        if prior is not None and prior.get("state") in HANDED_OFF:
            outcome = prior["state"] if prior["state"] in ("played", "cut_off") else "lost"
            speech.state = outcome
            self._log(f"agent speech {speech.id} delivery={speech.delivery_id} attempt={attempt} was handed off "
                      f"before ({prior['state']}): not spoken again")
            self.sink.ack(speech.delivery_id, speech.lease_id, outcome, device_id, detail="handed off before")
            return speech.id
        if self.sink.live and not speech.delivery_id.startswith("fx_"):
            speech.state = "accepting"
            speech.proactive_id = uuid.uuid4().hex
            asyncio.get_running_loop().create_task(self._accept_then_queue(speech))
        else:
            self._queue_speech(speech)
        return speech.id

    def _record_handoff(self, speech):
        """Records the attempt's hand-off state; returns whether the state file holds it."""
        if speech.delivery_id.startswith("fx_"):
            return True
        self.handoffs[speech.delivery_id] = {"attempt": speech.attempt, "state": speech.state}
        self.handoffs.move_to_end(speech.delivery_id)
        return self._save()

    async def _accept_then_queue(self, speech):
        result = await self.sink.accept(speech.delivery_id, speech.lease_id, speech.device,
                                        proactive_id=speech.proactive_id, ttl_s=speech.ttl)
        if speech.state != "accepting":
            return
        if result != "accepted":
            speech.state = "stale" if result == "stale" else "unaccepted"
            self._log(f"agent speech {speech.id} delivery={speech.delivery_id} not played: agent-service "
                      f"{'refused the lease' if result == 'stale' else 'did not record the hand-off'}")
            return
        speech.state = "handed_off"
        if not self._record_handoff(speech):
            self.handoffs.pop(speech.delivery_id, None)
            speech.state = "accepting"
            self._log(f"agent speech {speech.id} delivery={speech.delivery_id} not played: hand-off not written "
                      f"to the state file")
            self._speech_final(speech, "deferred", retry_after_ms=DEFERRED_RETRY_MS, detail="hand-off not recorded")
            return
        self._queue_speech(speech)

    def _queue_speech(self, speech):
        from weixue_proactive import RequestError

        fields = {
            "mode": "announce", "text": speech.text[:500], "context": "", "device": speech.device,
            "urgent": speech.urgent, "key": f"{speech.delivery_id}:{speech.attempt}", "ttl": speech.ttl,
            "agent": {"speech": speech.id},
        }
        if speech.proactive_id:
            fields["id"] = speech.proactive_id
        accepted = speech.state == "handed_off"
        try:
            status, body = self.proactive.submit(fields, SPEECH_CLIENT)
        except RequestError as e:
            retry_ms = int(e.body.get("retry_after", 5)) * 1000
            self._speech_final(speech, "deferred", retry_after_ms=retry_ms, detail=e.body.get("error"))
            return
        if body.get("duplicate"):
            self._speech_final(speech, "lost", detail="proactive key already used")
            return
        speech.proactive_id = body["id"]
        speech.state = "handed_off"
        speech.started = self.clock()
        self._log(f"agent speech {speech.id} handed off: delivery={speech.delivery_id} proactive={body['id']} "
                  f"urgent={speech.urgent} attempt={speech.attempt} reason={body.get('reason') or '-'}")
        if not accepted:
            self.sink.ack(speech.delivery_id, speech.lease_id, "accepted", speech.device,
                          proactive_id=body["id"], ttl_s=speech.ttl)

    def _speech_final(self, speech, outcome, **detail):
        """The one terminal ack of a speech attempt: later playback evidence changes nothing.
        deferred asks agent-service (the only retry owner) to offer the delivery again, up to
        SPEECH_ATTEMPTS attempts."""
        if speech.state in SPEECH_FINAL:
            return False
        if outcome == "deferred" and speech.attempt >= SPEECH_ATTEMPTS:
            outcome = "dropped"
            detail.pop("retry_after_ms", None)
        handed = speech.state == "handed_off"
        speech.state = outcome
        if handed and outcome in HANDED_OFF:
            self._record_handoff(speech)
        elif handed:
            self.handoffs.pop(speech.delivery_id, None)
            self._save()
        self.sink.ack(speech.delivery_id, speech.lease_id, outcome, speech.device, **detail)
        return True

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
        if speech.state in SPEECH_FINAL:
            self._log(f"agent played for sentence {event['sentence_id']} after speech {speech.id} ended "
                      f"({speech.state}): ignored")
            return
        speech.played = event
        outcome = "played" if event["complete"] else "cut_off"
        self._log(f"agent speech {speech.id} {outcome}: sentence={event['sentence_id']} "
                  f"cut_at_ms={event.get('cut_at_ms')} reason={event.get('reason')}")
        self._spoken(speech)
        self._speech_final(speech, outcome, sentence_id=event["sentence_id"], boot=event["boot"],
                           device_seq=event["seq"], cut_at_ms=event.get("cut_at_ms"), reason=event.get("reason"))
        if outcome == "played" and speech.item_state in RESULT_STATES:
            self._mark_seen(device_id, speech.task_id)

    def _mark_seen(self, device_id, task_id):
        """The device played a result whole: its mirrored result item goes again with seen:true."""
        dev = self.devices.get(device_id)
        entry = dev.mirror.get(task_id) if dev is not None else None
        if entry is None or entry.payload.get("state") not in RESULT_STATES or entry.payload.get("seen"):
            return None
        fields = {k: v for k, v in entry.payload.items() if k not in ("kind", "v", "seq", "ts")}
        fields.update(seen=True, delivery="silent", speaks=False)
        item = Entry(payload=wire.build_item(fields, self._next_seq(dev), int(self.wall() * 1000)),
                     delivery_id=entry.delivery_id, gseq=entry.gseq, born=entry.born)
        dev.mirror[task_id] = item
        dev.outgoing.append(item)
        if item.delivery_id and not item.delivery_id.startswith(("fx_", "gw_")):
            self.wire_seqs[item.delivery_id] = item.seq
            self._save()
        self._log(f"agent item seen device={device_id} task={task_id} state={item.payload['state']} seq={item.seq} "
                  f"delivery={item.delivery_id}: played")
        self._dispatch(device_id, dev, [item])
        return item

    def _withdraw_card_speech(self, device_id, card_id, why):
        """Speech of a decided or replaced card that has not started playing is withdrawn from the
        proactive queue and acked dropped."""
        if not card_id:
            return
        for speech in list(self.speeches.values()):
            if speech.device != device_id or speech.card_id != card_id or speech.state in SPEECH_FINAL:
                continue
            msg = self.proactive.messages.get(speech.proactive_id) if speech.proactive_id else None
            if msg is not None and not self.proactive.withdraw(msg.id, why):
                continue
            self._log(f"agent speech {speech.id} delivery={speech.delivery_id} card={card_id} withdrawn: {why}")
            self._speech_final(speech, "dropped", detail=why)

    def on_interrupted(self, conn):
        """An INTERRUPT item stops the session's playback: the agent sentence playing on it is cut
        off (reason superseded) before the device hears the next one."""
        sentence_id = getattr(conn, "_weixue_ack_sentence", None)
        speech = self._by_sentence(sentence_id) if sentence_id else None
        if speech is None or speech.state in SPEECH_FINAL:
            return
        speech.played = {"complete": False, "reason": "superseded", "sentence_id": sentence_id}
        self._log(f"agent speech {speech.id} cut_off: sentence={sentence_id} interrupted by an INTERRUPT item")
        self._speech_final(speech, "cut_off", sentence_id=sentence_id, reason="superseded")

    def _spoken(self, speech):
        if not speech.spoken_acked and speech.state not in SPEECH_FINAL:
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
            if speech.state != "handed_off":
                continue
            msg = self.proactive.messages.get(speech.proactive_id)
            status = msg.status if msg is not None else None
            if status in ("delivered", "partial"):
                if speech.ended_at is None:
                    speech.ended_at = now
                self._spoken(speech)
            elif status in ("failed", "expired"):
                # Both end before any audio was sent (proactive delivery rules).
                if status == "failed":
                    self._speech_final(speech, "deferred", retry_after_ms=DEFERRED_RETRY_MS, detail=msg.error or status)
                else:
                    self._speech_final(speech, "dropped", detail=status)
                continue
            elif msg is None:
                self._speech_final(speech, "lost", detail="proactive record gone")
                continue
            reference = speech.stop_at if speech.stop_at is not None else speech.ended_at
            if speech.played is None and reference is not None and now - reference >= LOST_SECONDS:
                self._log(f"agent speech {speech.id} lost: no played ack {LOST_SECONDS}s after playback ended")
                self._speech_final(speech, "lost", sentence_id=speech.sentence_id)

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
                    resend.append(e.payload)
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
            resume = getattr(self.sink, "resume", None)
            if resume is not None:
                resume()

    # Reconcile

    @staticmethod
    def _open_cards(dev):
        """Mirrored agent-service open cards: task id → entry."""
        return {t: e for t, e in dev.mirror.items() if e.payload.get("state") in OPEN_CARD_STATES
                and e.delivery_id and not e.delivery_id.startswith(("fx_", "gw_"))}

    def _snapshot_ready(self, dev):
        return dev.snapshot_due and dev.restored and (dev.reconciling is None or dev.reconciling.done())

    def _reconcile_first(self, device_id, dev):
        """Starts a reconcile of the device's open cards that the next snapshot waits for; returns
        whether one runs."""
        if dev.reconciling is not None and not dev.reconciling.done():
            return True
        if getattr(self.sink, "get_task", None) is None or not dev.restored or not self._open_cards(dev):
            return False

        async def run():
            try:
                await self.reconcile(device_id)
            finally:
                dev.reconciling = None
                if dev.snapshot_due:
                    self._dispatch(device_id, dev, [])
                self.proactive._notify()

        dev.reconciling = asyncio.get_running_loop().create_task(run())
        return True

    async def reconcile(self, device_id):
        """Checks the device's open cards against agent-service (GET /v1/tasks/{id}): a task it
        reports ended gets its terminal item, one it does not know is dropped, and a running task
        whose card's interaction closed gets a working item. Without an answer the mirror stays.
        Returns the reconciled task ids."""
        fetch = getattr(self.sink, "get_task", None)
        dev = self.device(device_id)
        cards = self._open_cards(dev)
        if fetch is None or not cards:
            return []
        results = await asyncio.gather(*(fetch(t) for t in cards))
        unanswered = sum(r is None for r in results)
        if unanswered and not self._reconcile_failing:
            self.logger.bind(tag=TAG).warning(
                f"mirror reconcile device={device_id}: agent-service did not answer for {unanswered} of "
                f"{len(cards)} open cards; mirror kept")
        self._reconcile_failing = unanswered > 0
        changed, drops = [], []
        for (task, entry), result in zip(cards.items(), results):
            if result is None or dev.mirror.get(task) is not entry:
                continue
            found, body = result
            state = body.get("state") if body else None
            pending = {p.get("interaction_id") for p in (body or {}).get("pending") or [] if isinstance(p, dict)}
            if found == "unknown":
                reason, replace = "unknown", None
            elif state in SERVICE_TERMINAL:
                reason, replace = state, state
            elif state == "progress" and entry.payload.get("card_id") not in pending:
                reason, replace = "card closed", "working"
            else:
                continue
            self._log(f"mirror reconcile dropped task={task} reason={reason} device={device_id} "
                      f"card={entry.payload.get('card_id')} now={replace or 'dropped'}")
            self._discard_outgoing(device_id, dev, task)
            if replace is None:
                drops.append(task)
            else:
                self._replace_card(device_id, dev, entry, replace, body.get("summary"))
            changed.append(task)
        if drops:
            self.drop(device_id, drops)
        if changed and dev.restored:
            refresh = self.refresh_chats(device_id)
            if refresh is not None:
                await refresh
        return changed

    def _discard_outgoing(self, device_id, dev, task):
        """Removes the task's unsent items; their deliveries are acked dropped."""
        for e in [e for e in dev.outgoing if not e.raw and e.payload.get("task_id") == task]:
            dev.outgoing.remove(e)
            if e.lease_id:
                self.sink.ack(e.delivery_id, e.lease_id, "dropped", device_id, detail="card closed in agent-service")

    def _replace_card(self, device_id, dev, entry, state, summary):
        """Mirrors and queues the task's state in place of its open card."""
        p = entry.payload
        self._withdraw_card_speech(device_id, p.get("card_id"), f"card closed in agent-service ({state})")
        fields = {"chat_id": p["chat_id"], "task_id": p["task_id"], "state": state, "summary": summary or "",
                  "lang": p.get("lang"), "delivery": "silent"}
        if p.get("title"):
            fields["title"] = p["title"]
        item = Entry(payload=wire.build_item(fields, self._next_seq(dev), int(self.wall() * 1000)),
                     delivery_id=f"gw_{uuid.uuid4().hex[:24]}", gseq=entry.gseq)
        dev.mirror[p["task_id"]] = item
        dev.outgoing.append(item)
        self._dispatch(device_id, dev, [item])

    # agent-service outbox stream

    def watch_device(self, device_id):
        """Starts the agent-service stream for a device once (live mode only)."""
        if not self.service_url or device_id in self._sources:
            return
        self._sources[device_id] = asyncio.get_running_loop().create_task(self._stream(device_id))

    async def restore(self, device_id):
        """Restores the device's mirror and chat list from the agent-service device snapshot
        (cards only, never speech). Restored cards keep the wire seq the device applied when the
        state file has it; a card the mirror holds in a newer version (gseq) stays. Pruning
        snapshots wait for this. Returns whether the snapshot arrived."""
        dev = self.device(device_id)
        if dev.restored:
            return True
        snapshot = await self.sink.get_snapshot(device_id)
        if snapshot is None:
            return False
        restored = 0
        for card in sorted(snapshot.get("cards") or [], key=lambda c: c.get("gseq") or 0):
            if card.get("kind") != "agent" or not card.get("delivery_id"):
                continue
            try:
                fields, _speech = wire.from_delivery(dict(card, speak=None))
                seq = self.wire_seqs.get(card["delivery_id"]) or self._next_seq(dev)
                dev.seq = max(dev.seq, seq)
                entry = Entry(payload=wire.build_item(fields, seq, int(self.wall() * 1000)),
                              delivery_id=card["delivery_id"], gseq=card.get("gseq"))
            except (wire.WireError, KeyError, TypeError) as e:
                self._log(f"agent restore device={device_id} delivery={card.get('delivery_id')} skipped: {e}")
                continue
            task = entry.payload["task_id"]
            latest = dev.mirror.get(task)
            if latest is not None and (latest.gseq is None or (entry.gseq or 0) <= latest.gseq):
                continue
            if _ended(latest, entry.payload):
                continue
            if latest is None and len(dev.mirror) >= MIRROR_TASKS:
                victim = _eviction_victim(dev.mirror)
                if victim is None:
                    continue
                del dev.mirror[victim]
            dev.mirror[task] = entry
            dev.mirror.move_to_end(task)
            restored += 1
        await self.reconcile(device_id)
        dev.restored = True
        self._log(f"agent restore device={device_id}: {restored} cards from the agent-service snapshot; "
                  f"mirror {len(dev.mirror)}")
        if isinstance(snapshot.get("chats"), dict):
            self.set_chats(device_id, snapshot["chats"])
        if dev.snapshot_due:
            self._dispatch(device_id, dev, [])
        self.proactive._notify()
        return True

    async def _stream(self, device_id):
        import aiohttp

        url = f"{self.service_url.rstrip('/')}/v1/deliveries/stream"
        headers = {"Authorization": f"Bearer {self.service_token}", "Accept": "text/event-stream"}
        backoff = 1
        while True:
            try:
                if self.device(device_id).restored:
                    await self.reconcile(device_id)
                elif not await self.restore(device_id):
                    raise RuntimeError("device snapshot unavailable")
                async with aiohttp.ClientSession(headers=headers) as http:
                    async with http.get(url, params={"device_id": device_id}, timeout=None) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"HTTP {resp.status}")
                        backoff = 1
                        self.refresh_chats(device_id)
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
                "restored": dev.restored,
                "agent_sessions": len(self.live_sessions(dev)),
                "polls_with_seq": dev.polls_with_seq,
                "level": dev.level,
                "chats": dev.chats.payload if dev.chats is not None else None,
                "mirror": [{"task_id": t, "seq": e.seq, "state": e.payload.get("state"), "delivery_id": e.delivery_id}
                           for t, e in dev.mirror.items()],
                "unacked": [{"seq": e.seq, "task_id": e.payload.get("task_id"), "sends": e.sends, "raw": e.raw}
                            for e in dev.outgoing],
            }
        speeches = [
            {"speech_id": s.id, "delivery_id": s.delivery_id, "task_id": s.task_id, "state": s.state,
             "urgent": s.urgent,
             "proactive_id": s.proactive_id, "sentence_id": s.sentence_id, "attempt": s.attempt, "played": s.played}
            for s in self.speeches.values()
        ]
        return {"devices": devices, "speeches": speeches, "acks": list(self.sink.acks),
                "responses": list(self.sink.responses),
                "unsent_acks": sum(len(v) for v in getattr(self.sink, "pending_acks", {}).values())}

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
                gateway.on_agent_session(device_id, conn, agent)
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
        state = weixue_proactive.StateFile(weixue_proactive.state_path("agent.json"), logger)
        INSTANCE = AgentGateway(proactive, sink=sink, state=state)
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
