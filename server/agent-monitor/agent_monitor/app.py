"""FastAPI app: GET-only JSON API, the static page and an SSE stream of changed sections."""
import asyncio
import contextlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Optional, Set
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import queries, tiles
from .config import Settings
from .db import DbUnavailable, ReadOnlyDb
from .live import LiveSources

STATIC = Path(__file__).parent / "static"
DB_POLL_S = 1.0
LIVE_POLL_S = 2.0
HEARTBEAT_S = 15.0
HOME_PERIOD_S = 300
CLIENT_QUEUE = 64
TIMELINE_PAGE = 30
SECTIONS = ("overview", "timeline", "cost", "chats", "errors", "home")
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _epoch(ts: Optional[str]) -> Optional[float]:
    parsed = queries._parse(ts)
    return parsed.timestamp() if parsed else None


class Board:
    """Builds every section from the database and the live sources."""

    def __init__(self, settings: Settings, db: ReadOnlyDb, live: LiveSources, clock: Callable[[], float]):
        self.settings = settings
        self.db = db
        self.live = live
        self.clock = clock
        self.tz = ZoneInfo(settings.tz)

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), timezone.utc)

    def db_state(self) -> Dict[str, Any]:
        state = self.db.state()
        version = self.db.schema_version()
        banner = None
        if state == "schema":
            banner = f"schema {version} not supported"
        elif state == "missing":
            banner = "agent database not readable"
        return {"state": state, "schema": version, "banner": banner}

    def _ha_probe(self, probes_db: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
        """The agent's latest in-memory probe while /v1/status answers, else the latest stored row."""
        status = self.live.status
        if status.data is not None and status.failures == 0:
            probe = (status.data.get("probes") or {}).get(name)
            if probe is not None:
                return probe
        row = probes_db.get(name)
        if row is None:
            return None
        return {"ok": row["ok"], "latency_ms": row["latency_ms"], "ts": _epoch(row["ts"]), **row["detail"]}

    def overview(self) -> Dict[str, Any]:
        now = self.clock()
        live = self.live
        db = self.db_state()
        probes_db: Dict[str, Any] = {}
        counts: Dict[str, Any] = {"open_tasks": None, "errors_24h": None, "cost_today_usd": None}
        pending = {"approvals": [], "interactions": []}
        cache = None
        if db["state"] == "ok":
            with contextlib.suppress(DbUnavailable):
                probes_db = queries.latest_probes(self.db)
                counts = {
                    "open_tasks": queries.open_tasks(self.db),
                    "errors_24h": queries.error_count(self.db, queries.iso(self.now() - timedelta(hours=24))),
                    "cost_today_usd": queries.cost_today(self.db, self.tz, self.now()),
                }
                pending = queries.pending(self.db)
                cache = queries.tools_cache(self.db)
        ha_probe = self._ha_probe(probes_db, "ha_mcp")
        tools_age = now - _epoch(cache["fetched_at"]) if cache and cache.get("fetched_at") else None
        ha_tile = tiles.ha_mcp(ha_probe, now, self.settings.ha_probe_s, tools_age)
        strip = [
            tiles.device(live.gateway.data if live.gateway.failures == 0 else None, live.gateway.error,
                         live.gateway.age(now)),
            tiles.agent(live.health.data, live.health.failures, live.health.error, live.health.age(now)),
            ha_tile,
            tiles.bridge(live.bridge.data, now, live.bridge.error),
            tiles.endpoint(live.endpoint.failures, (live.endpoint.data or {}).get("latency_ms"), live.endpoint.error,
                           live.endpoint.age(now), enabled=bool(self.settings.mcp_endpoint)),
            tiles.tunnel(self.settings.tunnel, self._ha_probe(probes_db, "tunnel"), now, self.settings.ha_probe_s),
        ]
        status = live.status.data or {}
        ha_status = status.get("ha_mcp") or {}
        probe = ha_probe or {}
        approvals = []
        for row in pending["approvals"]:
            created, expires = _epoch(row["created_at"]), _epoch(row["ha_expires_at"])
            approvals.append({**row, "age_s": round(now - created) if created else None,
                              "expires_in_s": round(expires - now) if expires else None})
        mcp = {
            "state": ha_tile["state"],
            "reachable": bool(probe.get("ok")) and ha_tile["state"] != "bad",
            "tool_count": probe.get("tool_count", ha_status.get("tool_count")),
            "policy_version": probe.get("policy_version", ha_status.get("policy_version")),
            "wait_seconds": probe.get("wait_seconds", ha_status.get("wait_seconds")),
            "pending_count": probe.get("pending_count"),
            "pending_not_ours": probe.get("pending_not_ours"),
            "pending_ours": approvals,
            "interactions_pending": pending["interactions"],
            "server": ha_status.get("server"),
            "tools_cache": cache,
            "probe_age_s": ha_tile["age_s"],
        }
        return {"tiles": [t for t in strip if t["state"] != "off"], "mcp": mcp, "counts": counts, "db": db,
                "agent": {k: status.get(k) for k in ("open_tasks", "turns_in_flight", "open_holds", "journal_mode")},
                "tz": self.settings.tz, "home_enabled": self.settings.home, "tunnel": self.settings.tunnel,
                "now": queries.iso(self.now())}

    def _readable(self) -> bool:
        return self.db.state() == "ok"

    def timeline(self, **kwargs: Any) -> Dict[str, Any]:
        if not self._readable():
            return {"groups": [], "next_before": None}
        return queries.timeline(self.db, **kwargs)

    def cost(self, group: str = "chat", days: int = 30) -> Dict[str, Any]:
        if not self._readable():
            return {"group": group, "days": days, "totals": {}, "by_day": [], "rows": []}
        return queries.cost(self.db, group, days, self.tz, self.now())

    def cost_section(self) -> Dict[str, Any]:
        if not self._readable():
            return {"totals": {}, "by_day": [], "chats": [], "tasks": []}
        chats = queries.cost(self.db, "chat", 30, self.tz, self.now())
        tasks = queries.cost(self.db, "task", 30, self.tz, self.now())
        return {"totals": chats["totals"], "by_day": chats["by_day"], "chats": chats["rows"][:50],
                "tasks": tasks["rows"][:50], "tz": self.settings.tz}

    def chats(self) -> Dict[str, Any]:
        if not self._readable():
            return {"chats": []}
        rows = queries.chats(self.db)
        actors = {a["chat_id"]: a for a in (self.live.status.data or {}).get("actors") or []}
        for row in rows:
            actor = actors.get(row["chat_id"])
            row["busy"] = bool(actor and actor.get("busy"))
            row["queued"] = actor.get("queued") if actor else 0
        return {"chats": rows}

    def errors(self, limit: int = 100) -> Dict[str, Any]:
        if not self._readable():
            return {"errors": []}
        return {"errors": queries.errors(self.db, limit)}

    def home(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"enabled": self.settings.home, "entities": [], "fetched_at": None, "stale": None}
        if not self.settings.home or not self._readable():
            return out
        rows = queries.home(self.db)
        if rows is None:
            out["code"] = "no_snapshot_table"
            return out
        fetched = max((r["fetched_at"] for r in rows if r.get("fetched_at")), default=None)
        out.update(entities=rows, fetched_at=fetched,
                   stale=fetched is None or self.clock() - _epoch(fetched) > 2 * HOME_PERIOD_S)
        return out

    def section(self, name: str) -> Dict[str, Any]:
        builders = {"overview": self.overview, "timeline": lambda: self.timeline(limit=TIMELINE_PAGE),
                    "cost": self.cost_section, "chats": self.chats, "errors": lambda: self.errors(50),
                    "home": self.home}
        return builders[name]()


class Hub:
    """Polls PRAGMA data_version every DB_POLL_S and the live sources every LIVE_POLL_S; pushes changed sections."""

    def __init__(self, board: Board):
        self.board = board
        self.cache: Dict[str, str] = {}
        self.clients: Set[asyncio.Queue] = set()
        self._task: Optional[asyncio.Task] = None
        self._data_version: Any = object()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="monitor-hub")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task

    async def _build(self, names) -> None:
        for name in names:
            try:
                data = await run_in_threadpool(self.board.section, name)
            except DbUnavailable:
                continue
            self.publish(name, data)

    def publish(self, name: str, data: Dict[str, Any]) -> None:
        body = json.dumps({"section": name, "data": data}, separators=(",", ":"), default=str)
        if self.cache.get(name) == body:
            return
        self.cache[name] = body
        frame = f"event: section\ndata: {body}\n\n"
        for queue in list(self.clients):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                self.clients.discard(queue)

    async def db_tick(self) -> bool:
        """Rebuilds the database sections when another connection committed; True when it did."""
        version = await run_in_threadpool(self.board.db.data_version)
        if version == self._data_version:
            return False
        self._data_version = version
        await self._build(SECTIONS)
        return True

    async def live_tick(self) -> None:
        await self.board.live.refresh()
        await self._build(("overview", "chats"))

    async def _run(self) -> None:
        last_live = 0.0
        while True:
            with contextlib.suppress(Exception):
                if time.monotonic() - last_live >= LIVE_POLL_S:
                    last_live = time.monotonic()
                    await self.live_tick()
                await self.db_tick()
            await asyncio.sleep(DB_POLL_S)

    async def stream(self, request: Optional[Request] = None) -> AsyncIterator[str]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=CLIENT_QUEUE)
        missing = [n for n in SECTIONS if n not in self.cache]
        if missing:
            await self._build(missing)
        self.clients.add(queue)
        try:
            for name in SECTIONS:
                if name in self.cache:
                    yield f"event: section\ndata: {self.cache[name]}\n\n"
            while queue in self.clients:
                if request is not None and await request.is_disconnected():
                    break
                try:
                    yield await asyncio.wait_for(queue.get(), HEARTBEAT_S)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            self.clients.discard(queue)


def create_app(settings: Settings, db: Optional[ReadOnlyDb] = None, live: Optional[LiveSources] = None,
               clock: Callable[[], float] = time.time, background: bool = True) -> FastAPI:
    db = db or ReadOnlyDb(settings.db_path)
    live = live or LiveSources(settings, clock=clock)
    board = Board(settings, db, live, clock)
    hub = Hub(board)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        if background:
            hub.start()
        try:
            yield
        finally:
            await hub.stop()
            await live.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.board = board
    app.state.hub = hub

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    def guarded(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DbUnavailable:
            raise HTTPException(503, "agent database not readable") from None

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz")
    def healthz():
        state = board.db_state()
        sources = {name: ("ok" if src.failures == 0 and src.data is not None else src.error or "pending")
                   for name, src in (("agent", live.health), ("status", live.status), ("gateway", live.gateway),
                                     ("endpoint", live.endpoint), ("bridge", live.bridge))}
        body = {"ok": state["state"] == "ok", "db": state["state"], "schema": state["schema"], "sources": sources}
        return JSONResponse(body, status_code=200 if body["ok"] else 503)

    @app.get("/api/overview")
    def overview():
        return guarded(board.overview)

    @app.get("/api/timeline")
    def timeline(task: Optional[str] = None, chat: Optional[str] = None, state: Optional[str] = None,
                 kind: Optional[str] = None, before: Optional[str] = None, limit: int = Query(100, ge=1, le=200)):
        if task:
            detail = guarded(queries.task_detail, db, task) if board._readable() else None
            return {"groups": [detail] if detail else [], "next_before": None}
        return guarded(board.timeline, chat=chat, state=state, kind=kind, before=before, limit=limit)

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str):
        detail = guarded(queries.task_detail, db, task_id) if board._readable() else None
        if detail is None:
            raise HTTPException(404, "task not found")
        return detail

    @app.get("/api/cost")
    def cost(group: str = Query("chat", pattern="^(chat|task|day)$"), days: int = Query(30, ge=1, le=3660)):
        return guarded(board.cost, group, days)

    @app.get("/api/home")
    def home():
        return guarded(board.home)

    @app.get("/api/chats")
    def chats():
        return guarded(board.chats)

    @app.get("/api/errors")
    def errors(limit: int = Query(100, ge=1, le=500)):
        return guarded(board.errors, limit)

    @app.get("/api/stream")
    async def stream(request: Request):
        return StreamingResponse(hub.stream(request), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
