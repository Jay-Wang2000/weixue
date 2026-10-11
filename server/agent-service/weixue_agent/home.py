"""Home snapshot: a capped set of Home Assistant entities read through the HA gateway on a fixed period.

Every call is an ordinary read-only ha-mcp call through `HaGateway.call`, so the ha-mcp policy decides held
versus free. A call that comes back held is withdrawn where possible and the cycle fails. A failed cycle never
raises and never touches the stored rows.
"""
import asyncio
import contextlib
import json
import time
from typing import Any, Dict, List, Optional, Sequence

from . import jsonlog
from .config import HOME_ENTITIES_MAX
from .ha_adapter import HaGateway
from .store import Store, dumps

HOME_DOMAINS = ("lock", "climate", "cover", "light")
KEY_ATTRIBUTES = {
    "light": ("brightness",),
    "climate": ("current_temperature", "temperature", "hvac_action"),
    "cover": ("current_position",),
    "lock": (),
}
OTHER_ATTRIBUTES = ("unit_of_measurement",)
NAME_MAX = 64
STATE_MAX = 32
FAILURE_RETRY_S = 60
FIRST_RUN_DELAY_S = 5


class SnapshotError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _payload(result: Any) -> Any:
    """The JSON document of a tool result's text content."""
    text = "\n".join(getattr(c, "text", "") or "" for c in result.content if getattr(c, "type", "") == "text")
    if text.startswith("PENDING_APPROVAL"):
        raise SnapshotError("held")
    if result.is_error:
        raise SnapshotError("ha_unreachable" if "unreachable" in text else "ha_error")
    try:
        return json.loads(text)
    except ValueError:
        raise SnapshotError("bad_response") from None


def _find(value: Any, key: str) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        if isinstance(value.get(key), dict):
            return value[key]
        for item in value.values():
            found = _find(item, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find(item, key)
            if found is not None:
                return found
    return None


def domain_of(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def select_entities(payload: Any) -> List[str]:
    """Entity ids of an ha_get_overview result, locks first, then climate, covers and lights, each by id."""
    stats = _find(payload, "domain_stats")
    if stats is None:
        raise SnapshotError("bad_response")
    chosen: List[str] = []
    for domain in HOME_DOMAINS:
        entities = (stats.get(domain) or {}).get("entities") or []
        ids = sorted({e["entity_id"] for e in entities if isinstance(e, dict) and isinstance(e.get("entity_id"), str)
                      and domain_of(e["entity_id"]) == domain})
        chosen.extend(ids)
    return chosen[:HOME_ENTITIES_MAX]


def trim_attributes(entity_id: str, attributes: Any) -> Dict[str, Any]:
    keys = KEY_ATTRIBUTES.get(domain_of(entity_id), OTHER_ATTRIBUTES)
    if not isinstance(attributes, dict):
        return {}
    return {k: attributes[k] for k in keys if attributes.get(k) is not None}


def snapshot_rows(payload: Any, wanted: Sequence[str]) -> List[Dict[str, Any]]:
    """One row per requested entity that the ha_get_state result returned a state for."""
    states = _find(payload, "states")
    if states is None:
        raise SnapshotError("bad_response")
    rows = []
    for entity_id in wanted:
        record = states.get(entity_id)
        if not isinstance(record, dict) or not isinstance(record.get("state"), str):
            continue
        attributes = record.get("attributes") if isinstance(record.get("attributes"), dict) else {}
        name = attributes.get("friendly_name")
        rows.append({
            "entity_id": entity_id,
            "name": str(name)[:NAME_MAX] if name else None,
            "state": record["state"][:STATE_MAX],
            "attributes_json": dumps(trim_attributes(entity_id, attributes)),
            "last_changed": record.get("last_changed") if isinstance(record.get("last_changed"), str) else None,
        })
    return rows


def state_request(entity_ids: Sequence[str]) -> Dict[str, Any]:
    keys = {"friendly_name"}
    for entity_id in entity_ids:
        keys.update(KEY_ATTRIBUTES.get(domain_of(entity_id), OTHER_ATTRIBUTES))
    return {"entity_id": list(entity_ids), "fields": ["entity_id", "state", "attributes", "last_changed"],
            "attribute_keys": sorted(keys)}


class HomeSnapshot:
    def __init__(self, store: Store, ha: HaGateway, period_s: float, entities: Sequence[str] = ()):
        self.store = store
        self.ha = ha
        self.period_s = period_s
        self.entities = tuple(entities)
        self.calls = 0
        self.held = 0
        self._task: Optional[asyncio.Task] = None
        self._last_error: Optional[str] = None

    def start(self) -> None:
        if self.period_s > 0:
            self._task = asyncio.create_task(self._loop(), name="home-snapshot")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task

    async def _loop(self) -> None:
        delay = FIRST_RUN_DELAY_S
        while True:
            await asyncio.sleep(delay)
            started = time.monotonic()
            ok = await self.run_once()
            delay = max(0.0, self.period_s - (time.monotonic() - started)) if ok else min(self.period_s, FAILURE_RETRY_S)

    async def _call(self, tool: str, args: Dict[str, Any]) -> Any:
        self.calls += 1
        waiter = {"device_id": None, "chat_id": None, "task_id": None, "turn_id": None,
                  "tool_use_id": f"home-{self.calls}"}
        result = await self.ha.call(waiter, tool, args)
        disposition = self.ha.take_disposition(waiter["tool_use_id"])
        if disposition and disposition[1] == "pending":
            self.held += 1
            if disposition[0]:
                with contextlib.suppress(Exception):
                    await self.ha.withdraw(disposition[0], via="home_snapshot")
            raise SnapshotError("held")
        return _payload(result)

    async def _fetch(self) -> List[Dict[str, Any]]:
        if self.entities:
            wanted = list(self.entities)
        else:
            wanted = select_entities(await self._call("ha_get_overview", {
                "domains": list(HOME_DOMAINS), "detail_level": "full", "max_entities_per_domain": 0,
                "include_notifications": False, "fields": ["domain_stats"]}))
        if not wanted:
            return []
        return snapshot_rows(await self._call("ha_get_state", state_request(wanted)), wanted)

    async def run_once(self) -> bool:
        """True when the snapshot was read; a failure is stored and logged, never raised."""
        started = time.monotonic()
        calls_before, held_before = self.calls, self.held
        code: Optional[str] = None
        written = 0
        count = 0
        try:
            rows = await self._fetch()
            count = len(rows)
            written = self.store.replace_home_snapshot(rows, self.period_s)
        except SnapshotError as e:
            code = e.code
        except asyncio.CancelledError:
            raise
        except Exception as e:
            code = "error"
            jsonlog.log("home snapshot error", level="warning", code=type(e).__name__)
        with contextlib.suppress(Exception):
            self.store.record_home_attempt(self.period_s, code, count)
        if code is not None and code != self._last_error:
            with contextlib.suppress(Exception):
                self.store.record_error("home_snapshot", code)
        self._last_error = code
        jsonlog.log("home snapshot", level="info" if code is None else "warning", event="home_snapshot",
                    outcome="ok" if code is None else code,
                    detail=(f"entities={count} written={written} calls={self.calls - calls_before} "
                            f"held={self.held - held_before}"),
                    latency_ms=int((time.monotonic() - started) * 1000))
        return code is None
