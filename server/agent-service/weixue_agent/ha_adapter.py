"""Home Assistant through ha-mcp with the ADR-9 pending model.

Every ha-mcp tool reaches the turn loop through `HaSession.call_tool`; the ha-mcp policy alone decides
held versus free. A call that has not returned after HA_CALL_BUDGET_S answers PENDING_APPROVAL while
the original request keeps waiting (up to the policy's wait_seconds). A held call is owned only through
the token its own request returned (`token_source = 'response'`); `/api/policy/pending` is observed,
never used to claim a hold. Only owned holds are decided through `/api/policy/approve|deny`. Every call
ends with a disposition (executed, failed, not_sent, denied, expired, unknown, pending, blocked); an
unknown write is never retried and blocks an identical write until the original can no longer run.
"""
import asyncio
import contextlib
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Set, Tuple

from mcp.types import CallToolResult, TextContent, Tool

from weixue_hamcp import SESSION_EXPIRED, HaMcpClient, HaMcpError, PolicyError

from . import jsonlog, policy_model, timeutil
from .config import (
    ACTION_MAX, HA_CALL_BUDGET_S, HA_IDLE_POLL_S, HA_MCP_FALLBACK_WAIT_S, HA_UNKNOWN_FALLBACK_TTL_S,
    HA_PENDING_POLL_S, HA_TOOLS_REFRESH_S, HA_TOOLS_RETRY_MAX_S, HA_TOOLS_RETRY_MIN_S, Settings,
)
from .ids import new_id
from .store import Store, dumps, loads

OPEN_STATES = ("in_flight", "pending", "awaiting_decision_after_wait", "replay_dispatched")
HOLD_STATES = ("pending", "awaiting_decision_after_wait")
TERMINAL_STATES = ("executed", "failed", "not_sent", "denied", "expired", "unknown")
NOT_SENT_ERRORS = ("ConnectError", "ConnectTimeout")
DISPOSITIONS_KEPT = 256
UNREACHABLE_TEXT = "Home Assistant is unreachable right now."
UNKNOWN_TEXT = ("The Home Assistant call ended without a result; it may or may not have run. "
                "Do not call it again.")
BLOCKED_TEXT = ("Not sent: an identical Home Assistant call ended with an unknown result and may still run or "
                "have run. Do not call it again; check the state with a read-only tool and tell the user.")
GUARD_TEXT = ("Refused by the end-to-end test guard: this call would change Home Assistant without approval. "
              "Do not retry it.")
REVERSIBLE_DOMAINS = {"light", "switch", "fan", "input_boolean", "media_player", "cover", "climate"}
REVERSIBLE_SERVICES = {"turn_on", "turn_off", "toggle", "open_cover", "close_cover", "media_play", "media_pause",
                       "volume_set", "set_temperature"}


class ApprovalListener(Protocol):
    def on_pending(self, approval: Dict[str, Any], waiter: Dict[str, Any]) -> None: ...
    def on_correlated(self, approval: Dict[str, Any]) -> None: ...
    def on_terminal(self, approval: Dict[str, Any]) -> None: ...


class Uncorrelated(Exception):
    """The held call is not provably ours (no token from its own request): only the Home Assistant panel
    decides it."""


class AlreadyDecided(Exception):
    pass


class NotOwner(Exception):
    pass


def canonical(args: Dict[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def args_hash(args: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical(args).encode()).hexdigest()


def result_text(result: Dict[str, Any]) -> str:
    return "\n".join(c.get("text", "") for c in result.get("content") or []
                     if isinstance(c, dict) and c.get("type") == "text")


def _find_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value and isinstance(value[key], (str, int, float)):
            return value[key]
        for v in value.values():
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for v in value:
            found = _find_key(v, key)
            if found is not None:
                return found
    return None


def parse_error(text: str) -> Tuple[Optional[str], Optional[str], Optional[float]]:
    """(code, token, expires_in_seconds) from an ha-mcp error result text."""
    code = None
    for candidate in ("USER_DENIED", "USER_APPROVAL_REQUIRED"):
        if candidate in text:
            code = candidate
    token = expires = None
    start = text.find("{")
    if start >= 0:
        with contextlib.suppress(ValueError):
            data = json.loads(text[start:])
            token = _find_key(data, "token")
            expires = _find_key(data, "expires_in_seconds")
    return code, (str(token) if token is not None else None), (float(expires) if expires is not None else None)


def describe_call(tool: str, args: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """Card text (≤60) and target of a call."""
    if tool == "ha_call_service":
        target = args.get("entity_id") or (args.get("data") or {}).get("entity_id") \
            or (args.get("target") or {}).get("entity_id")
        if isinstance(target, list):
            target = ", ".join(map(str, target))
        text = f"{args.get('domain', '?')}.{args.get('service', '?')}" + (f" {target}" if target else "")
        return jsonlog.truncate(text, ACTION_MAX), jsonlog.truncate(str(target), 128) if target else None
    config = args.get("config") if isinstance(args.get("config"), dict) else {}
    name = config.get("alias") or args.get("name") or args.get("entity_id") or args.get("identifier")
    label = tool.removeprefix("ha_").replace("_", " ")
    text = f"{label}: {name}" if name else label
    return jsonlog.truncate(text, ACTION_MAX), jsonlog.truncate(str(name), 128) if name else None


def reversible(tool: str, args: Dict[str, Any]) -> bool:
    """Only plain on/off-style service calls on household devices count as reversible (decision 31)."""
    if tool != "ha_call_service":
        return False
    return str(args.get("domain", "")).lower() in REVERSIBLE_DOMAINS and \
        str(args.get("service", "")).lower() in REVERSIBLE_SERVICES


def tunnel_iface_present(name: str = "wg0") -> bool:
    return (Path("/sys/class/net") / name).exists()


class HaGateway:
    def __init__(self, settings: Settings, store: Store, client: Optional[HaMcpClient],
                 require_iface: Optional[str] = None, iface_present: Callable[[str], bool] = tunnel_iface_present):
        self.settings = settings
        self.store = store
        self.client = client
        self.listener: Optional[ApprovalListener] = None
        self.on_tools_changed: Optional[Callable[[], None]] = None
        self.require_iface = require_iface
        self._iface_present = iface_present
        self.tools: List[Dict[str, Any]] = []
        self.read_only: Set[str] = set()
        self.policy: Dict[str, Any] = {}
        self.last_ok: Optional[float] = None
        self.last_ping_ok: Optional[float] = None
        self.last_error: Optional[str] = None
        self.last_pending_count: Optional[int] = None
        self.last_pending: List[Dict[str, Any]] = []
        self._inflight: Dict[str, asyncio.Task] = {}
        self._abandoned: Set[str] = set()
        self._background: Set[asyncio.Task] = set()
        self._loops: List[asyncio.Task] = []
        self._poll_wakeup = asyncio.Event()
        self._dispositions: Dict[str, Tuple[Optional[str], str]] = {}

    # ------------------------------------------------------------------ lifecycle

    def _spawn(self, coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    @staticmethod
    async def _quiet(coro) -> None:
        with contextlib.suppress(Exception):
            await coro

    def load_cache(self) -> None:
        row = self.store.one("SELECT tools_json FROM ha_tools_cache WHERE id = 1")
        if row:
            self._set_tools(loads(row["tools_json"], []))

    def _set_tools(self, tools: List[Dict[str, Any]]) -> bool:
        changed = [t.get("name") for t in tools] != [t.get("name") for t in self.tools] or tools != self.tools
        self.tools = tools
        self.read_only = {t["name"] for t in tools if (t.get("annotations") or {}).get("readOnlyHint")}
        return changed

    async def start(self) -> None:
        self.load_cache()
        if self.client is None:
            return
        first = self._spawn(self._quiet(self.refresh()), "ha-first-refresh")
        if not self.tools:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(first), timeout=25)
        self._loops.append(asyncio.create_task(self._refresh_loop(), name="ha-refresh"))
        self._loops.append(asyncio.create_task(self._poll_loop(), name="ha-poll"))

    async def stop(self) -> None:
        for task in self._loops + list(self._background) + list(self._inflight.values()):
            task.cancel()
        for task in self._loops + list(self._background) + list(self._inflight.values()):
            with contextlib.suppress(BaseException):
                await task
        if self.client is not None:
            await self.client.aclose()

    async def refresh(self) -> None:
        """tools/list and the policy config over the held session; the tool list is cached in SQLite."""
        try:
            await self.client.ensure_session()
            tools = await self.client.list_tools()
            self.policy = await self.client.policy_config()
        except (HaMcpError, PolicyError) as e:
            self.last_error = str(e)
            jsonlog.log("ha-mcp refresh failed", level="warning", code=str(e), detail=self.client.session)
            raise
        changed = False
        if tools:
            changed = self._set_tools(tools)
            self.store.execute(
                "INSERT INTO ha_tools_cache (id, tools_json, fetched_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET tools_json = excluded.tools_json, fetched_at = excluded.fetched_at",
                (dumps(tools), timeutil.iso()))
            if changed and self.on_tools_changed:
                self.on_tools_changed()
        self.last_ok = time.time()
        timing = self.client.timings.get("tools/list")
        jsonlog.log("ha-mcp tools refreshed", event="ha_tools", outcome=str(len(self.tools)),
                    detail=(f"tools={len(self.tools)} bytes={timing.size if timing else -1} "
                            f"first_byte_ms={timing.first_byte_ms if timing else -1} "
                            f"total_ms={timing.total_ms if timing else -1} session={self.client.session} "
                            f"sessions_opened={self.client.sessions_opened} changed={int(changed)} "
                            f"policy=v{self.policy.get('version')}"))

    async def _refresh_loop(self) -> None:
        """Refresh every HA_TOOLS_REFRESH_S; a failed refresh retries after 5 s, doubling up to 300 s."""
        delay = HA_TOOLS_REFRESH_S if self.tools else HA_TOOLS_RETRY_MIN_S
        while True:
            await asyncio.sleep(delay)
            try:
                await self.refresh()
                delay = HA_TOOLS_REFRESH_S
            except Exception:
                delay = HA_TOOLS_RETRY_MIN_S if delay >= HA_TOOLS_REFRESH_S else min(HA_TOOLS_RETRY_MAX_S, delay * 2)

    async def ping(self, timeout_s: Optional[float] = None) -> bool:
        """A matching successful MCP ping result within timeout_s; anything else is unhealthy."""
        if self.client is None:
            return False
        try:
            await asyncio.wait_for(self.client.request("ping", {}), timeout=timeout_s)
        except HaMcpError as e:
            self.last_error = str(e)
            return False
        except asyncio.TimeoutError:
            self.last_error = f"ping timed out after {timeout_s:g} s"
            return False
        self.last_ping_ok = self.last_ok = time.time()
        return True

    @property
    def wait_seconds(self) -> float:
        return float(self.policy.get("wait_seconds") or HA_MCP_FALLBACK_WAIT_S)

    @property
    def approval_ttl_s(self) -> Optional[float]:
        minutes = self.policy.get("approval_ttl_minutes")
        return float(minutes) * 60 if minutes else None

    @property
    def unknown_hold_s(self) -> float:
        """How long an unknown write may still run in ha-mcp: the policy wait plus the approval TTL."""
        return self.wait_seconds + (self.approval_ttl_s or HA_UNKNOWN_FALLBACK_TTL_S)

    @staticmethod
    def owned(row: Optional[Dict[str, Any]]) -> bool:
        return bool(row and row["ha_token"] and row["token_source"] == "response")

    def take_disposition(self, tool_use_id: Optional[str]) -> Optional[Tuple[Optional[str], str]]:
        """(approval_id, disposition) of a finished `call` for this tool use, once."""
        return self._dispositions.pop(tool_use_id, None) if tool_use_id else None

    def _dispose(self, waiter: Dict[str, Any], approval_id: Optional[str], disposition: str) -> None:
        if waiter.get("tool_use_id"):
            self._dispositions[waiter["tool_use_id"]] = (approval_id, disposition)
            while len(self._dispositions) > DISPOSITIONS_KEPT:
                self._dispositions.pop(next(iter(self._dispositions)))

    def is_read_only(self, tool: str) -> bool:
        return tool in self.read_only

    def _reachable(self) -> bool:
        if self.client is None:
            return False
        return self.require_iface is None or self._iface_present(self.require_iface)

    # ------------------------------------------------------------------ session surface for the turn loop

    def session(self, resolve: Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]) -> "HaSession":
        """`resolve(tool, args)` returns the caller identity {device_id, chat_id, task_id, turn_id, tool_use_id}."""
        return HaSession(self, resolve)

    def mcp_tools(self, specs: Optional[List[Dict[str, Any]]] = None) -> List[Tool]:
        tools = []
        for raw in self.tools if specs is None else specs:
            spec = {k: v for k, v in raw.items() if k not in ("outputSchema", "icons", "execution", "_meta")}
            with contextlib.suppress(Exception):
                tools.append(Tool.model_validate(spec))
        return tools

    # ------------------------------------------------------------------ calls

    def _guard_allows(self, tool: str, args: Dict[str, Any]) -> bool:
        rules = self.policy.get("rules") or []
        if policy_model.held(rules, tool, args):
            return True
        if self.is_read_only(tool):
            return True
        return tool == "ha_call_service" and args.get("entity_id") == "light.living_room_ceiling_lights"

    def pending_text(self, approval: Dict[str, Any]) -> str:
        text, _ = describe_call(approval["tool"], loads(approval["args_json"], {}))
        return (f"PENDING_APPROVAL id={approval['approval_id']}: Home Assistant has not answered {text} yet; it may "
                "be holding it for approval or still running it. Do not call it again; the outcome will arrive as "
                "a follow-up message.")

    @staticmethod
    def _scrubbed(result: CallToolResult) -> CallToolResult:
        """A tool result as the model and the transcript get it: secrets redacted."""
        data = result.model_dump(exclude_none=True)
        return CallToolResult.model_validate(jsonlog.scrub(data))

    def _disposition_of(self, approval_id: str) -> str:
        row = self.store.approval(approval_id)
        state = row["state"] if row else "unknown"
        return "pending" if state in OPEN_STATES else state

    @staticmethod
    def _text_result(text: str, is_error: bool = False) -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text=text)], isError=is_error)

    async def call(self, waiter: Dict[str, Any], tool: str, args: Dict[str, Any]) -> CallToolResult:
        result, approval_id = await self._call(waiter, tool, args)
        if approval_id is not None:
            self._dispose(waiter, approval_id, self._disposition_of(approval_id))
        return self._scrubbed(result)

    async def _call(self, waiter: Dict[str, Any], tool: str,
                    args: Dict[str, Any]) -> Tuple[CallToolResult, Optional[str]]:
        if not self._reachable():
            self._dispose(waiter, None, "not_sent")
            return self._text_result(UNREACHABLE_TEXT, True), None
        if self.settings.e2e_guard and not self._guard_allows(tool, args):
            jsonlog.log("e2e guard refused a free call", level="warning", task_id=waiter.get("task_id"), tool=tool)
            self._dispose(waiter, None, "not_sent")
            return self._text_result(GUARD_TEXT, True), None
        digest = args_hash(args)
        existing = self.store.one(
            "SELECT * FROM approvals WHERE tool = ? AND args_hash = ? AND state IN ('in_flight', 'pending', "
            "'awaiting_decision_after_wait', 'replay_dispatched') ORDER BY created_at DESC LIMIT 1", (tool, digest))
        if existing is None and not self.is_read_only(tool):
            since = timeutil.iso(timeutil.from_epoch(time.time() - self.unknown_hold_s))
            unresolved = self.store.one(
                "SELECT approval_id FROM approvals WHERE tool = ? AND args_hash = ? AND state = 'unknown' "
                "AND updated_at > ? ORDER BY updated_at DESC LIMIT 1", (tool, digest, since))
            if unresolved:
                jsonlog.log("ha call blocked by an unknown identical call", level="warning", tool=tool,
                            task_id=waiter.get("task_id"), event="ha_blocked", detail=unresolved["approval_id"])
                self._dispose(waiter, unresolved["approval_id"], "blocked")
                return self._text_result(BLOCKED_TEXT, True), None
        if existing and existing["state"] != "in_flight":
            waiters = loads(existing["waiters_json"], []) + [waiter]
            self.store.update("approvals", "approval_id", existing["approval_id"],
                              {"waiters_json": dumps(waiters), "updated_at": timeutil.iso()})
            existing = self.store.approval(existing["approval_id"])
            if self.listener:
                self.listener.on_pending(existing, waiter)
            return self._text_result(self.pending_text(existing)), existing["approval_id"]
        if existing:
            send = self._inflight.get(existing["approval_id"])
            if send is not None:
                waiters = loads(existing["waiters_json"], []) + [waiter]
                self.store.update("approvals", "approval_id", existing["approval_id"],
                                  {"waiters_json": dumps(waiters), "updated_at": timeutil.iso()})
                return (await self._await_send(existing["approval_id"], send, waiter, time.monotonic()),
                        existing["approval_id"])
        now = timeutil.iso()
        approval_id = new_id("apr")
        self.store.insert("approvals", {
            "approval_id": approval_id, "device_id": waiter.get("device_id"), "task_id": waiter.get("task_id"),
            "chat_id": waiter.get("chat_id"), "turn_id": waiter.get("turn_id"), "tool_use_id": waiter.get("tool_use_id"),
            "tool": tool, "args_json": dumps(args), "args_hash": digest, "state": "in_flight",
            "waiters_json": dumps([waiter]), "created_at": now, "updated_at": now,
        })
        started = time.monotonic()
        send = asyncio.create_task(self._send(approval_id, tool, args, started), name=f"ha:{approval_id}")
        self._inflight[approval_id] = send
        send.add_done_callback(lambda _t: self._inflight.pop(approval_id, None))
        return await self._await_send(approval_id, send, waiter, started), approval_id

    async def _await_send(self, approval_id: str, send: asyncio.Task, waiter: Dict[str, Any],
                          started: float) -> CallToolResult:
        try:
            done, _ = await asyncio.wait({send}, timeout=HA_CALL_BUDGET_S)
        except asyncio.CancelledError:
            self._abandoned.add(approval_id)
            jsonlog.log("ha call abandoned by its turn", task_id=waiter.get("task_id"), turn_id=waiter.get("turn_id"),
                        event="ha_abandoned", detail=approval_id)
            raise
        if send in done:
            return send.result()
        row = self.store.approval(approval_id)
        if row["state"] == "in_flight":
            self.store.update("approvals", "approval_id", approval_id, {"state": "pending", "updated_at": timeutil.iso()})
            row = self.store.approval(approval_id)
            jsonlog.log("ha call pending", task_id=waiter.get("task_id"), chat_id=waiter.get("chat_id"), tool=row["tool"],
                        event="approval_pending", detail=approval_id,
                        latency_ms=int((time.monotonic() - started) * 1000))
            if self.listener:
                self.listener.on_pending(row, waiter)
            self._poll_wakeup.set()
        return self._text_result(self.pending_text(row))

    async def _send(self, approval_id: str, tool: str, args: Dict[str, Any], started: float) -> CallToolResult:
        try:
            result = await self.client.call_tool(tool, args, read_timeout_s=self.wait_seconds + 20)
        except HaMcpError as e:
            name = str(e)
            state = "not_sent" if name in NOT_SENT_ERRORS or name == SESSION_EXPIRED else "unknown"
            self._finish(approval_id, state, UNREACHABLE_TEXT if state == "not_sent" else f"result unknown ({name})",
                         started)
            return self._text_result(UNREACHABLE_TEXT if state == "not_sent" else UNKNOWN_TEXT, True)
        except asyncio.CancelledError:
            self._finish(approval_id, "unknown", "service stopped", started)
            raise
        self.last_ok = time.time()
        text = result_text(result)
        if result.get("isError"):
            code, token, expires_in = parse_error(text)
            if code == "USER_APPROVAL_REQUIRED":
                return self._awaiting(approval_id, token, expires_in)
            if code == "USER_DENIED":
                self._finish(approval_id, "denied", "denied", started)
                return self._text_result("Home Assistant: the user denied this action. Do not retry it.", True)
            self._finish(approval_id, "failed", text, started)
        else:
            self._finish(approval_id, "executed", text, started)
        return CallToolResult.model_validate({k: v for k, v in result.items() if k in ("content", "structuredContent", "isError")})

    def _awaiting(self, approval_id: str, token: Optional[str], expires_in: Optional[float]) -> CallToolResult:
        """The wait elapsed without a decision: the token our own request returned makes the hold ours."""
        row = self.store.approval(approval_id)
        values: Dict[str, Any] = {"state": "awaiting_decision_after_wait", "updated_at": timeutil.iso()}
        if token:
            values.update(ha_token=token, token_source="response")
        ttl = expires_in or self.approval_ttl_s
        if ttl:
            values["ha_expires_at"] = timeutil.iso(timeutil.from_epoch(time.time() + ttl))
        self.store.update("approvals", "approval_id", approval_id, values)
        updated = self.store.approval(approval_id)
        if self.listener:
            if row["state"] == "in_flight":
                for waiter in loads(updated["waiters_json"], []):
                    self.listener.on_pending(updated, waiter)
            if token:
                self.listener.on_correlated(updated)
        self._claim_replay(approval_id)
        self._poll_wakeup.set()
        return self._text_result(self.pending_text(self.store.approval(approval_id)))

    def _finish(self, approval_id: str, state: str, text: str, started: float) -> None:
        row = self.store.approval(approval_id)
        if row is None or row["state"] in TERMINAL_STATES:
            return
        notify = row["state"] in HOLD_STATES or row["state"] == "replay_dispatched" or approval_id in self._abandoned
        self._abandoned.discard(approval_id)
        self.store.update("approvals", "approval_id", approval_id, {
            "state": state, "result_text": jsonlog.truncate(jsonlog.redact_text(text or ""), 500),
            "args_json": dumps(jsonlog.redact_args(loads(row["args_json"], {}))), "updated_at": timeutil.iso()})
        jsonlog.log("ha call finished", tool=row["tool"], outcome=state, task_id=row["task_id"], chat_id=row["chat_id"],
                    latency_ms=int((time.monotonic() - started) * 1000), detail=approval_id, event="ha_call")
        if notify and self.listener:
            self.listener.on_terminal(self.store.approval(approval_id))

    # ------------------------------------------------------------------ pending listing (observed only)

    def open_holds(self) -> List[Dict[str, Any]]:
        return self.store.all("SELECT * FROM approvals WHERE state IN ('pending', 'awaiting_decision_after_wait')")

    async def _poll_loop(self) -> None:
        while True:
            interval = HA_PENDING_POLL_S if self.open_holds() else HA_IDLE_POLL_S
            self._poll_wakeup.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._poll_wakeup.wait(), timeout=interval)
            with contextlib.suppress(Exception):
                await self.poll_once()

    async def poll_once(self) -> List[Dict[str, Any]]:
        """The ha-mcp pending listing for the monitor and expiry; it never makes a hold ours."""
        try:
            pending = await self.client.pending()
        except (HaMcpError, PolicyError) as e:
            self.last_error = str(e)
            raise
        self.last_ok = time.time()
        self.last_pending_count = len(pending)
        self.last_pending = pending
        self._expire_overdue()
        if self.listener is not None and hasattr(self.listener, "on_poll"):
            self.listener.on_poll(self.open_holds())
        return pending

    def _expire_overdue(self) -> None:
        now = timeutil.iso()
        for row in self.store.all("SELECT * FROM approvals WHERE state = 'awaiting_decision_after_wait' "
                                  "AND ha_expires_at IS NOT NULL AND ha_expires_at < ?", (now,)):
            self._finish(row["approval_id"], "expired", "approval lapsed", time.monotonic())

    # ------------------------------------------------------------------ decisions (route B)

    async def decide(self, approval_id: str, decision: str, via: str, device_id: Optional[str]) -> Dict[str, Any]:
        row = self.store.approval(approval_id)
        if row is None:
            raise KeyError(approval_id)
        if row["state"] in TERMINAL_STATES or row["decision"]:
            raise AlreadyDecided(row["decision"] or row["state"])
        if device_id is not None and row["device_id"] != device_id:
            raise NotOwner()
        if not self.owned(row):
            raise Uncorrelated()
        try:
            if decision == "approve":
                await self.client.approve(row["ha_token"])
            else:
                await self.client.deny(row["ha_token"])
            applied = decision
        except PolicyError as e:
            if e.status == 404:
                self._record_decision(approval_id, decision, via)
                self._finish(approval_id, "expired", "token unknown or expired", time.monotonic())
                return self.store.approval(approval_id)
            if e.status == 409:
                current = str(e.body.get("current_decision") or "")
                applied = "approve" if current.startswith("approv") else "deny" if current.startswith("den") else decision
            else:
                raise
        except HaMcpError:
            self._record_decision(approval_id, decision, via)
            self._finish(approval_id, "unknown", "decision response lost", time.monotonic())
            return self.store.approval(approval_id)
        self._record_decision(approval_id, applied, via)
        if applied == "approve":
            self._claim_replay(approval_id)
        elif self.store.approval(approval_id)["state"] == "awaiting_decision_after_wait":
            self._finish(approval_id, "denied", "denied on the weixue panel", time.monotonic())
        return self.store.approval(approval_id)

    def _claim_replay(self, approval_id: str) -> bool:
        """The single replay of an owned hold: needs our successful approve and the original's wait-elapsed
        answer, whichever is recorded second claims it."""
        claimed = self.store.execute(
            "UPDATE approvals SET state = 'replay_dispatched', updated_at = ? WHERE approval_id = ? "
            "AND state = 'awaiting_decision_after_wait' AND decision = 'approve' AND token_source = 'response'",
            (timeutil.iso(), approval_id))
        if claimed:
            self._spawn(self._replay(approval_id), f"replay:{approval_id}")
        return bool(claimed)

    def _record_decision(self, approval_id: str, decision: str, via: str) -> None:
        now = timeutil.iso()
        self.store.update("approvals", "approval_id", approval_id,
                          {"decision": decision, "via": via, "decided_at": now, "updated_at": now})
        jsonlog.log("ha hold decided", event="approval_decision", outcome=decision, detail=approval_id, code=via)

    async def _replay(self, approval_id: str) -> None:
        """One replay of the identical call after our own approve succeeded (ha-mcp consumes the approval)."""
        row = self.store.approval(approval_id)
        started = time.monotonic()
        args = loads(row["args_json"], {})
        try:
            result = await self.client.call_tool(row["tool"], args, read_timeout_s=self.wait_seconds + 20)
        except HaMcpError as e:
            self._finish(approval_id, "unknown", f"replay result unknown ({e})", started)
            return
        text = result_text(result)
        if result.get("isError"):
            code, _, _ = parse_error(text)
            self._finish(approval_id, "denied" if code == "USER_DENIED" else "failed",
                         "held again after approval" if code == "USER_APPROVAL_REQUIRED" else text, started)
        else:
            self._finish(approval_id, "executed", text, started)

    async def withdraw(self, approval_id: str, via: str = "api") -> str:
        """Cancel path: deny an owned hold so it can never run later; 'uncorrelated' when not provably ours."""
        row = self.store.approval(approval_id)
        if row is None or row["state"] not in HOLD_STATES:
            return "closed"
        if row["decision"]:
            return "decided"
        if not self.owned(row):
            return "uncorrelated"
        with contextlib.suppress(Exception):
            await self.decide(approval_id, "deny", via, None)
        return "denied"

    async def boot_reconcile(self) -> List[Dict[str, Any]]:
        """Rows left by a previous process: deny the listed tokens their own requests returned; everything else
        ends unknown and is never decided."""
        rows = self.store.all("SELECT * FROM approvals WHERE state IN "
                              "('in_flight', 'pending', 'awaiting_decision_after_wait', 'replay_dispatched')")
        if not rows:
            return []
        pending: List[Dict[str, Any]] = []
        if self.client is not None and self._reachable():
            try:
                pending = await self.client.pending()
            except Exception as e:
                jsonlog.log("boot reconcile: ha-mcp unreachable", level="warning", code=type(e).__name__)
        listed = {p.get("token") for p in pending}
        denied = 0
        for row in rows:
            row = self.store.approval(row["approval_id"])
            state, text = "unknown", "service restarted"
            if self.owned(row) and row["ha_token"] in listed and row["state"] in HOLD_STATES:
                try:
                    await self.client.deny(row["ha_token"])
                    state, text = "denied", "denied at service restart"
                    denied += 1
                except Exception:
                    pass
            self.store.update("approvals", "approval_id", row["approval_id"], {
                "state": state, "result_text": text, "updated_at": timeutil.iso(),
                "args_json": dumps(jsonlog.redact_args(loads(row["args_json"], {})))})
        jsonlog.log("boot reconcile", event="recovery", outcome=f"{len(rows)} holds", detail=f"{denied} denied")
        return [self.store.approval(r["approval_id"]) for r in rows]


class HaSession:
    """The `ClientSession`-shaped surface `async_mcp_tool` calls: every call goes through the 8 s boundary."""

    def __init__(self, gateway: HaGateway, resolve: Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]):
        self.gateway = gateway
        self.resolve = resolve

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None, **_: Any) -> CallToolResult:
        args = dict(arguments or {})
        waiter = await self.resolve(name, args)
        return await self.gateway.call(waiter, name, args)
