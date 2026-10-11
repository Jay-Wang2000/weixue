"""Durable device outbox: tiers, slots, leases, two-phase acks and playback evidence.

One consumer per device: a new stream revokes the previous one. Offers are ordered INTERRUPT,
WHEN_IDLE, SILENT, then gseq; every ack echoes the offer's lease_id (and attempt). The service is the only
retry owner: a deferred delivery is offered again with a new attempt and lease. Acks only move a delivery
forward (ACK_FROM); one that does not fits its state changes nothing. Speech that may have started
without a played/cut_off ack is never offered again (its card is marked "may not have played").
"""
import asyncio
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional

from . import jsonlog, timeutil
from .config import DELIVERY_LEASE_S, DONE_SPEECH_TTL_S
from .ids import new_id
from .store import Store, loads_or

TIERS = {"INTERRUPT": 0, "WHEN_IDLE": 1, "SILENT": 2}
OPEN_STATES = ("pending", "offered", "handed_off")
SNAPSHOT_HIDDEN = ("superseded", "dropped", "expired")
ACK_FROM = {
    "accepted": ("offered",),
    "spoken": ("offered", "handed_off"),
    "played": ("offered", "handed_off", "uncertain"),
    "cut_off": ("offered", "handed_off", "uncertain"),
    "shown": ("pending", "offered"),
    "deferred": ("offered", "handed_off"),
    "lost": ("offered", "handed_off"),
    "dropped": ("pending", "offered", "handed_off"),
}


class StaleLease(Exception):
    pass


def agent_payload(task: Dict[str, Any], state: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    """Card body of a task; state names are the task's (the gateway maps them to the wire)."""
    payload = {
        "task_id": task["task_id"], "chat_id": task["chat_id"], "state": state or task["state"],
        "title": task.get("title"), "summary": task.get("summary"), "lang": task.get("lang"),
        "outcome": task.get("outcome"),
    }
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


class Outbox:
    def __init__(self, store: Store):
        self.store = store
        self._wakeups: Dict[str, asyncio.Event] = {}
        self._consumers: Dict[str, str] = {}

    def _wake(self, device_id: str) -> None:
        event = self._wakeups.get(device_id)
        if event is not None:
            event.set()

    def enqueue(self, device_id: str, kind: str, tier: str, slot: str, payload: Dict[str, Any],
                task_id: Optional[str] = None, chat_id: Optional[str] = None, turn_id: Optional[str] = None,
                speak: Optional[str] = None, lang: Optional[str] = None, ttl_s: Optional[float] = None) -> str:
        now = timeutil.now()
        if speak is None and tier != "SILENT":
            tier = "SILENT"
        payload = dict(payload, tier=tier, speaks=bool(speak))
        with self.store.tx():
            self.store.execute(
                "UPDATE deliveries SET state = 'superseded', updated_at = ? "
                "WHERE device_id = ? AND slot = ? AND state IN ('pending', 'offered')",
                (timeutil.iso(now), device_id, slot))
            delivery_id = new_id("dlv")
            self.store.insert("deliveries", {
                "delivery_id": delivery_id, "device_id": device_id, "task_id": task_id, "chat_id": chat_id,
                "turn_id": turn_id, "kind": kind, "tier": tier, "slot": slot, "payload_json": payload,
                "speak": speak, "lang": lang, "state": "pending", "attempt": 0,
                "expires_at": timeutil.iso(now + timedelta(seconds=ttl_s)) if ttl_s else None,
                "created_at": timeutil.iso(now), "updated_at": timeutil.iso(now),
            })
        jsonlog.log("delivery queued", device_id=device_id, task_id=task_id, chat_id=chat_id, event="delivery_offer",
                    outcome=f"{tier} {kind}", detail=f"{slot} {delivery_id}")
        self._wake(device_id)
        return delivery_id

    def withdraw(self, device_id: str, slot: str, spoken_only: bool = False) -> int:
        """Drop deliveries of a slot that have not been handed off."""
        sql = ("UPDATE deliveries SET state = 'superseded', updated_at = ? WHERE device_id = ? AND slot = ? "
               "AND state IN ('pending', 'offered')")
        if spoken_only:
            sql += " AND speak IS NOT NULL"
        n = self.store.execute(sql, (timeutil.iso(), device_id, slot))
        if n:
            self._wake(device_id)
        return n

    def _mark_uncertain(self, row: Dict[str, Any], reason: str) -> None:
        self.store.update("deliveries", "delivery_id", row["delivery_id"], {
            "state": "uncertain", "playback": "uncertain", "updated_at": timeutil.iso()})
        jsonlog.log("delivery playback uncertain", device_id=row["device_id"], task_id=row["task_id"],
                    event="delivery_ack", outcome="uncertain", detail=f"{row['delivery_id']} {reason}")
        self._card_update(row, "uncertain")

    def _card_update(self, row: Dict[str, Any], playback: str) -> None:
        if row["kind"] != "agent":
            return
        payload = dict(loads_or(row["payload_json"], {}), playback=playback, unread=True)
        payload.pop("tier", None)
        payload.pop("speaks", None)
        self.enqueue(row["device_id"], "agent", "SILENT", row["slot"], payload, task_id=row["task_id"],
                     chat_id=row["chat_id"], turn_id=row["turn_id"], lang=row["lang"])

    def sweep(self) -> None:
        now = timeutil.iso()
        self.store.execute(
            "UPDATE deliveries SET state = 'expired', updated_at = ? WHERE state IN ('pending', 'offered') "
            "AND expires_at IS NOT NULL AND expires_at < ?", (now, now))
        self.store.execute(
            "UPDATE deliveries SET state = 'pending', lease_id = NULL, updated_at = ? "
            "WHERE state = 'offered' AND lease_until IS NOT NULL AND lease_until < ?", (now, now))
        for row in self.store.all("SELECT * FROM deliveries WHERE state = 'handed_off' AND lease_until < ?", (now,)):
            if row["speak"]:
                self._mark_uncertain(row, "lease passed after hand-off")
            else:
                self.store.update("deliveries", "delivery_id", row["delivery_id"], {"state": "delivered", "updated_at": now})

    def next_offers(self, device_id: str) -> List[Dict[str, Any]]:
        self.sweep()
        now = timeutil.iso()
        rows = self.store.all(
            "SELECT * FROM deliveries WHERE device_id = ? AND state = 'pending' "
            "AND (not_before IS NULL OR not_before <= ?) ORDER BY gseq", (device_id, now))
        rows.sort(key=lambda r: (TIERS.get(r["tier"], 9), r["gseq"]))
        return rows

    def offer(self, row: Dict[str, Any]) -> Dict[str, Any]:
        lease_id = str(uuid.uuid4())
        lease_until = timeutil.iso(timeutil.now() + timedelta(seconds=DELIVERY_LEASE_S))
        self.store.update("deliveries", "delivery_id", row["delivery_id"], {
            "state": "offered", "lease_id": lease_id, "lease_until": lease_until,
            "attempt": max(1, row["attempt"]), "updated_at": timeutil.iso()})
        return self.public(self.store.delivery(row["delivery_id"]))

    @staticmethod
    def public(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "delivery_id": row["delivery_id"], "gseq": row["gseq"], "lease_id": row["lease_id"],
            "device_id": row["device_id"], "task_id": row["task_id"], "chat_id": row["chat_id"],
            "turn_id": row["turn_id"], "kind": row["kind"], "tier": row["tier"], "slot": row["slot"],
            "payload": loads_or(row["payload_json"], {}), "speak": row["speak"], "lang": row["lang"],
            "attempt": row["attempt"], "idempotency_key": f"{row['delivery_id']}:{max(1, row['attempt'])}",
            "urgent": row["tier"] == "INTERRUPT", "expires_at": row["expires_at"], "created_at": row["created_at"],
            "state": row["state"], "playback": row["playback"],
        }

    def ack(self, delivery_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        row = self.store.delivery(delivery_id)
        if row is None:
            raise KeyError(delivery_id)
        if row["lease_id"] is None or row["lease_id"] != body["lease_id"]:
            raise StaleLease()
        if body.get("attempt") is not None and int(body["attempt"]) != max(1, row["attempt"]):
            raise StaleLease()
        outcome = body["outcome"]
        now = timeutil.now()
        values: Dict[str, Any] = {"updated_at": timeutil.iso(now)}
        card_playback: Optional[str] = None
        allowed = ACK_FROM.get(outcome)
        if outcome == "accepted" and row["state"] == "handed_off":
            allowed = None
        elif allowed is not None and row["state"] not in allowed:
            if outcome == "accepted" or (outcome == "deferred" and row["playback"]):
                raise StaleLease()
            self._record_ack(row, body, now, f"ignored in {row['state']}")
            return self.public(row)
        if allowed is None:
            pass
        elif outcome == "accepted":
            ttl = int(body.get("ttl_s") or DELIVERY_LEASE_S)
            until = now + timedelta(seconds=ttl)
            expires = timeutil.parse(row["expires_at"]) if row["expires_at"] else None
            values.update(state="handed_off", lease_until=timeutil.iso(min(until, expires) if expires else until),
                          proactive_id=body.get("proactive_id"))
        elif outcome == "spoken":
            values.update(playback="spoken")
        elif outcome == "played":
            values.update(state="delivered", playback="played")
            if row["state"] == "uncertain":
                card_playback = "played"
        elif outcome == "cut_off":
            values.update(state="delivered", playback="cut_off")
            card_playback = "cut_off"
        elif outcome == "shown":
            if row["state"] in ("pending", "offered") and not row["speak"]:
                values.update(state="delivered")
        elif outcome == "deferred":
            if row["playback"]:
                raise StaleLease()
            delay_ms = int(body.get("retry_after_ms") or 0)
            values.update(state="pending", attempt=row["attempt"] + 1, lease_id=None, lease_until=None,
                          not_before=timeutil.iso(now + timedelta(milliseconds=delay_ms)) if delay_ms else None)
        elif outcome == "lost":
            values.update(state="uncertain", playback="uncertain")
            card_playback = "uncertain"
        elif outcome == "dropped":
            values.update(state="dropped")
        self.store.update("deliveries", "delivery_id", delivery_id, values)
        self._record_ack(row, body, now, body.get("detail"))
        if card_playback:
            self._card_update(row, card_playback)
        if outcome == "deferred":
            self._wake(row["device_id"])
        return self.public(self.store.delivery(delivery_id))

    def _record_ack(self, row: Dict[str, Any], body: Dict[str, Any], now: Any, detail: Optional[str]) -> None:
        self.store.insert("delivery_acks", {
            "delivery_id": row["delivery_id"], "device_id": row["device_id"], "outcome": body["outcome"],
            "boot": body.get("boot"), "device_seq": body.get("device_seq"), "sentence_id": body.get("sentence_id"),
            "cut_at_ms": body.get("cut_at_ms"), "reason": body.get("reason"), "proactive_id": body.get("proactive_id"),
            "detail": jsonlog.truncate(detail, 200), "received_at": timeutil.iso(now)})
        jsonlog.log("delivery ack", device_id=row["device_id"], task_id=row["task_id"], event="delivery_ack",
                    outcome=body["outcome"], detail=f"{row['delivery_id']} {detail or ''}".strip())

    def on_restart(self) -> Dict[str, int]:
        """Unspoken offers return to pending; speech offered or handed off without a playback ack is never
        repeated (a lost acceptance ack is no evidence that it did not start)."""
        counts = {"reoffer": 0, "uncertain": 0}
        counts["reoffer"] = self.store.execute(
            "UPDATE deliveries SET state = 'pending', lease_id = NULL, updated_at = ? "
            "WHERE state = 'offered' AND speak IS NULL", (timeutil.iso(),))
        for row in self.store.all("SELECT * FROM deliveries WHERE state IN ('offered', 'handed_off')"):
            if row["speak"]:
                counts["uncertain"] += 1
                self._mark_uncertain(row, "service restart")
            else:
                self.store.update("deliveries", "delivery_id", row["delivery_id"],
                                  {"state": "delivered", "updated_at": timeutil.iso()})
        return counts

    def register_consumer(self, device_id: str) -> str:
        consumer = str(uuid.uuid4())
        previous = self._consumers.get(device_id)
        self._consumers[device_id] = consumer
        self._wakeups.setdefault(device_id, asyncio.Event()).set()
        if previous:
            self.store.execute("UPDATE deliveries SET state = 'pending', lease_id = NULL, updated_at = ? "
                               "WHERE device_id = ? AND state = 'offered'", (timeutil.iso(), device_id))
        return consumer

    def is_current(self, device_id: str, consumer: str) -> bool:
        return self._consumers.get(device_id) == consumer

    async def wait(self, device_id: str, timeout: float) -> None:
        event = self._wakeups.setdefault(device_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        event.clear()

    def snapshot(self, device_id: str) -> List[Dict[str, Any]]:
        """Latest card per slot from the last 24 h (pending or already shown)."""
        self.sweep()
        since = timeutil.iso(timeutil.now() - timedelta(seconds=DONE_SPEECH_TTL_S))
        rows = self.store.all(
            "SELECT * FROM deliveries WHERE device_id = ? AND created_at >= ? AND state NOT IN "
            f"({', '.join('?' for _ in SNAPSHOT_HIDDEN)}) ORDER BY gseq", (device_id, since, *SNAPSHOT_HIDDEN))
        latest: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            latest[row["slot"]] = row
        return [{**self.public(r), "speak": None} for r in latest.values()]
