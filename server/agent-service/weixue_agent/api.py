"""HTTP + SSE API (bearer token on every route but /healthz)."""
import hmac
import json
import re
from typing import Any, Dict, Optional

from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import jsonlog, timeutil
from .config import HEARTBEAT_REPLY_S, HEARTBEAT_S
from .deliveries import StaleLease
from .ids import trace_id as new_trace_id
from .schemas import (Ack, Answer, ChatCreate, ConnectionEvent, Decide, DeviceActionResult, DeviceRef, Interrupt,
                      MessageBody)
from .sse import HEARTBEAT_FRAME, sse_frame
from .store import loads
from .turns import ApiError, body_hash

TRACE_RE = re.compile(r"^[0-9a-f]{32}$")
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def error(status: int, code: str, message: str, detail: Any = None,
          headers: Optional[Dict[str, str]] = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "detail": detail}}, status_code=status,
                        headers=headers)


class Api:
    def __init__(self, service: Any):
        self.svc = service
        self.engine = service.engine
        self.store = service.store

    # ------------------------------------------------------------------ plumbing

    def _authorized(self, request: Request, read_scope: bool = False) -> bool:
        """The service token on every route; MONITOR_TOKEN also on read-scope routes."""
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return False
        presented = header[7:].strip().encode()
        if hmac.compare_digest(presented, self.svc.settings.token.encode()):
            return True
        monitor = self.svc.settings.monitor_token
        return read_scope and monitor is not None and hmac.compare_digest(presented, monitor.encode())

    async def _body(self, request: Request, model: type, optional: bool = False) -> BaseModel:
        raw = await request.body()
        if not raw and optional:
            return model.model_validate({})
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "invalid_request", "body is not JSON")
        try:
            return model.model_validate(data)
        except ValidationError as e:
            raise ApiError(400, "invalid_request", "body failed validation",
                           [{"loc": [str(p) for p in x["loc"]], "msg": x["msg"]} for x in e.errors()][:10])

    def route(self, handler, auth: bool = True, read_scope: bool = False):
        async def endpoint(request: Request) -> Response:
            trace = request.headers.get("x-trace-id", "").lower()
            request.state.trace_id = trace if TRACE_RE.match(trace) else new_trace_id()
            if auth and not self._authorized(request, read_scope and request.method == "GET"):
                return error(401, "unauthorized", "missing or wrong bearer token")
            try:
                response = await handler(request)
            except ApiError as e:
                response = error(e.status, e.code, e.message, e.detail, e.headers)
            except KeyError:
                response = error(404, "not_found", "not found")
            except Exception as e:
                jsonlog.log("api error", level="error", code=type(e).__name__, detail=jsonlog.truncate(str(e), 200),
                            event=request.url.path, trace_id=request.state.trace_id)
                self.store.record_error("api", "internal", jsonlog.truncate(str(e), 200), trace_id=request.state.trace_id)
                response = error(500, "internal", "internal error")
            response.headers["X-Trace-Id"] = request.state.trace_id
            return response
        return endpoint

    def routes(self):
        r = self.route
        return [
            Route("/healthz", r(self.healthz, auth=False)),
            Route("/v1/status", r(self.status, read_scope=True), methods=["GET"]),
            Route("/v1/chats", r(self.chats_list), methods=["GET"]),
            Route("/v1/chats", r(self.chats_create), methods=["POST"]),
            Route("/v1/chats/back", r(self.chats_back), methods=["POST"]),
            Route("/v1/chats/resolve", r(self.chats_resolve), methods=["GET"]),
            Route("/v1/chats/{chat_id}", r(self.chat_get), methods=["GET"]),
            Route("/v1/chats/{chat_id}/messages", r(self.messages), methods=["POST"]),
            Route("/v1/chats/{chat_id}/interrupt", r(self.interrupt), methods=["POST"]),
            Route("/v1/chats/{chat_id}/events", r(self.chat_events), methods=["GET"]),
            Route("/v1/chats/{chat_id}/approve", r(self.approve), methods=["POST"]),
            Route("/v1/chats/{chat_id}/deny", r(self.deny), methods=["POST"]),
            Route("/v1/chats/{chat_id}/answer", r(self.answer), methods=["POST"]),
            Route("/v1/chats/{chat_id}/device_actions/{action_id}", r(self.device_action), methods=["POST"]),
            Route("/v1/chats/{chat_id}/switch", r(self.chats_switch), methods=["POST"]),
            Route("/v1/chats/{chat_id}/read", r(self.chats_read), methods=["POST"]),
            Route("/v1/tasks/{task_id}", r(self.task_get), methods=["GET"]),
            Route("/v1/tasks/{task_id}/trace", r(self.task_trace, read_scope=True), methods=["GET"]),
            Route("/v1/deliveries/stream", r(self.deliveries_stream), methods=["GET"]),
            Route("/v1/deliveries/{delivery_id}/ack", r(self.deliveries_ack), methods=["POST"]),
            Route("/v1/devices/{device_id}/snapshot", r(self.device_snapshot), methods=["GET"]),
            Route("/v1/devices/{device_id}/connection", r(self.device_connection), methods=["POST"]),
        ]

    def app(self) -> Starlette:
        return Starlette(routes=self.routes())

    def _chat(self, chat_id: str) -> Dict[str, Any]:
        chat = self.store.chat(chat_id)
        if chat is None:
            raise ApiError(404, "not_found", "chat not found")
        return chat

    def _after(self, request: Request, default: int) -> int:
        raw = request.headers.get("last-event-id") or request.query_params.get("after")
        if raw is None:
            return default
        try:
            return max(0, int(raw))
        except ValueError:
            raise ApiError(400, "invalid_request", "after / Last-Event-ID must be an integer")

    # ------------------------------------------------------------------ views

    def public_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        pending = [self.engine.interactions.public(i) for i in self.engine.interactions.open_for_task(task["task_id"])]
        turns = self.store.all("SELECT turn_id FROM turns WHERE task_id = ? ORDER BY created_at", (task["task_id"],))
        run = self.engine.current_run(task["chat_id"])
        return {
            "task_id": task["task_id"], "chat_id": task["chat_id"], "device_id": task["device_id"],
            "state": task["state"], "title": task["title"], "summary": task["summary"], "lang": task["lang"],
            "progress": loads(task["progress_json"]), "pending": pending, "outcome": task["outcome"],
            "error": loads(task["error_json"]), "side_effects": loads(task["side_effects_json"], []),
            "turn_ids": [t["turn_id"] for t in turns],
            "current_turn_id": run.turn_id if run and run.task_id == task["task_id"] else None,
            "created_at": task["created_at"], "updated_at": task["updated_at"], "finished_at": task["finished_at"],
            "trace_id": task["trace_id"], "retry_of": task["retry_of"], "retry_allowed": bool(task["retry_allowed"]),
            "result_tier": task["result_tier"], "cost_usd": task["cost_usd"],
        }

    @staticmethod
    def public_turn(turn: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "turn_id": turn["turn_id"], "task_id": turn["task_id"], "chat_id": turn["chat_id"], "kind": turn["kind"],
            "state": "running" if turn["state"] == "queued" else turn["state"], "terminal_reason": turn["terminal_reason"],
            "started_at": turn["started_at"], "ended_at": turn["ended_at"], "reply_end_at": turn["reply_end_at"],
            "reply_end_reason": turn["reply_end_reason"], "interrupted_by": turn["interrupted_by"],
            "cost_usd": turn["cost_usd"], "ttft_ms": turn["ttft_ms"], "mode": turn["mode"], "model": turn["model"],
        }

    # ------------------------------------------------------------------ health

    async def healthz(self, request: Request) -> Response:
        health = self.svc.healthy()
        return JSONResponse(health, status_code=200 if health["ok"] else 503)

    async def status(self, request: Request) -> Response:
        return JSONResponse(self.svc.status())

    # ------------------------------------------------------------------ messages

    async def messages(self, request: Request) -> Response:
        key = request.headers.get("idempotency-key", "").strip()
        if not key or len(key) > 200:
            raise ApiError(400, "invalid_request", "Idempotency-Key header is required")
        body = (await self._body(request, MessageBody)).model_dump(exclude_none=True)
        response = await self.engine.handle_message(request.path_params["chat_id"], body, key, request.state.trace_id)
        headers = {"X-Turn-Id": response.get("turn_id") or "", "X-Chat-Id": response["chat_id"]}
        public = {k: response.get(k) for k in ("chat_id", "task_id", "turn_id", "routed", "duplicate")}
        if "text/event-stream" not in request.headers.get("accept", ""):
            return JSONResponse(public, status_code=200 if response.get("duplicate") else 201, headers=headers)
        after = self._after(request, response.get("after", 0))
        return self._message_stream(response, after, headers)

    def _message_stream(self, response: Dict[str, Any], after: int, headers: Dict[str, str]) -> StreamingResponse:
        until, _ = self.engine.stream_bounds(response)
        chat_id = response["chat_id"]
        engine = self.engine

        def heartbeat() -> float:
            return HEARTBEAT_REPLY_S if engine.reply_heartbeat(chat_id) else HEARTBEAT_S

        async def gen():
            finished = False
            try:
                async for event in self.svc.hub.stream(chat_id, after, until, heartbeat):
                    if event is None:
                        yield HEARTBEAT_FRAME
                        continue
                    if event["type"] == "text" and event["turn_id"] != response.get("turn_id"):
                        continue
                    yield sse_frame(event)
                    if until(event):
                        finished = True
            finally:
                if not finished:
                    engine.gateway_closed(chat_id, response["reply_id"])

        return StreamingResponse(gen(), media_type="text/event-stream", headers={**SSE_HEADERS, **headers})

    async def chat_events(self, request: Request) -> Response:
        chat_id = self._chat(request.path_params["chat_id"])["chat_id"]
        after = self._after(request, 0)
        engine = self.engine

        def heartbeat() -> float:
            return HEARTBEAT_REPLY_S if engine.reply_heartbeat(chat_id) else HEARTBEAT_S

        async def gen():
            async for event in self.svc.hub.stream(chat_id, after, lambda e: False, heartbeat):
                yield HEARTBEAT_FRAME if event is None else sse_frame(event)

        return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)

    async def interrupt(self, request: Request) -> Response:
        chat = self._chat(request.path_params["chat_id"])
        body = (await self._body(request, Interrupt)).model_dump(exclude_none=True)
        interactions = self.engine.interactions
        duplicate = interactions.check_decision(body["decision_id"], chat["chat_id"], "interrupt", body)
        if duplicate:
            return JSONResponse(duplicate)
        task = await self.engine.cancel(chat["chat_id"], body["reason"])
        response = {"chat_id": chat["chat_id"], "task": self.public_task(task) if task else None, "duplicate": False}
        interactions.store_decision(body["decision_id"], chat["chat_id"], "interrupt", body, response)
        return JSONResponse(response)

    async def approve(self, request: Request) -> Response:
        chat = self._chat(request.path_params["chat_id"])
        body = (await self._body(request, Decide)).model_dump(exclude_none=True)
        return JSONResponse(await self.engine.interactions.decide(chat["chat_id"], body, "approve"))

    async def deny(self, request: Request) -> Response:
        chat = self._chat(request.path_params["chat_id"])
        body = (await self._body(request, Decide)).model_dump(exclude_none=True)
        return JSONResponse(await self.engine.interactions.decide(chat["chat_id"], body, "deny"))

    async def answer(self, request: Request) -> Response:
        chat = self._chat(request.path_params["chat_id"])
        body = (await self._body(request, Answer)).model_dump(exclude_none=True)
        return JSONResponse(await self.engine.interactions.answer(chat["chat_id"], body))

    async def device_action(self, request: Request) -> Response:
        chat = self._chat(request.path_params["chat_id"])
        body = (await self._body(request, DeviceActionResult)).model_dump(exclude_none=True)
        return JSONResponse(self.engine.device_action_result(chat["chat_id"], request.path_params["action_id"], body))

    # ------------------------------------------------------------------ chats

    def _device_param(self, request: Request) -> str:
        device_id = request.query_params.get("device_id", "")
        if not device_id or len(device_id) > 64:
            raise ApiError(400, "invalid_request", "device_id is required")
        return device_id

    async def chats_list(self, request: Request) -> Response:
        device_id = self._device_param(request)
        try:
            limit = max(1, min(50, int(request.query_params.get("limit", "12"))))
        except ValueError:
            raise ApiError(400, "invalid_request", "limit must be an integer")
        return JSONResponse(self.svc.chats.listing(device_id, limit))

    async def chats_create(self, request: Request) -> Response:
        key = request.headers.get("idempotency-key", "").strip()
        if not key or len(key) > 200:
            raise ApiError(400, "invalid_request", "Idempotency-Key header is required")
        body = (await self._body(request, ChatCreate)).model_dump()
        digest = body_hash(body)
        existing = self.store.one("SELECT * FROM idempotency WHERE key = ?", (key,))
        if existing:
            if existing["request_hash"] != digest or existing["scope"] != "chat":
                raise ApiError(409, "idempotency_mismatch", "Idempotency-Key was used with a different body")
            return JSONResponse(dict(self.svc.chats.public(self.store.chat(existing["ref_id"])), duplicate=True))
        chat = self.svc.chats.create(body["device_id"], make_current=body["switch"])
        self.store.insert("idempotency", {"key": key, "scope": "chat", "request_hash": digest, "ref_id": chat["chat_id"],
                                          "created_at": timeutil.iso()}, or_ignore=True)
        return JSONResponse(dict(self.svc.chats.public(chat), duplicate=False), status_code=201)

    async def chat_get(self, request: Request) -> Response:
        chat = self._chat(request.path_params["chat_id"])
        out = self.svc.chats.public(chat)
        task = self.engine.open_task(chat["chat_id"])
        out["open_task"] = self.public_task(task) if task else None
        turns = self.store.all("SELECT * FROM turns WHERE chat_id = ? ORDER BY created_at DESC LIMIT 20", (chat["chat_id"],))
        out["turns"] = [self.public_turn(t) for t in turns]
        rows = self.store.all("SELECT role, state, rewritten FROM transcript WHERE chat_id = ? AND epoch = ? ORDER BY seq",
                              (chat["chat_id"], chat["epoch"]))
        out.update(epoch=chat["epoch"], mode=chat["mode"], task_model=chat["task_model"],
                   transcript=[{"role": r["role"], "state": r["state"], "rewritten": r["rewritten"]} for r in rows])
        return JSONResponse(out)

    async def chats_switch(self, request: Request) -> Response:
        chat = self._chat(request.path_params["chat_id"])
        device = self.svc.chats.switch(chat["device_id"], chat["chat_id"])
        self.engine.spawn(self.engine.prewarm(chat), f"prewarm:{chat['chat_id']}")
        return JSONResponse({"current_chat_id": device["current_chat_id"], "previous_chat_id": device["previous_chat_id"],
                             "last_summary": chat["summary"]})

    async def chats_back(self, request: Request) -> Response:
        body = (await self._body(request, DeviceRef)).model_dump()
        device = self.svc.chats.back(body["device_id"])
        if device is None:
            raise ApiError(409, "invalid_state", "there is no previous chat")
        chat = self.store.chat(device["current_chat_id"])
        if chat is not None:
            self.engine.spawn(self.engine.prewarm(chat), f"prewarm:{chat['chat_id']}")
        return JSONResponse({"current_chat_id": device["current_chat_id"], "previous_chat_id": device["previous_chat_id"],
                             "last_summary": chat["summary"] if chat else None})

    async def chats_resolve(self, request: Request) -> Response:
        device_id = self._device_param(request)
        query = request.query_params.get("q", "")
        return JSONResponse({"matches": self.svc.chats.resolve(device_id, query)})

    async def chats_read(self, request: Request) -> Response:
        chat = self._chat(request.path_params["chat_id"])
        self.svc.chats.mark_read(chat["chat_id"])
        return JSONResponse(self.svc.chats.public(self.store.chat(chat["chat_id"])))

    # ------------------------------------------------------------------ tasks

    def _task(self, task_id: str) -> Dict[str, Any]:
        task = self.store.task(task_id)
        if task is None:
            raise ApiError(404, "not_found", "task not found")
        return task

    async def task_get(self, request: Request) -> Response:
        return JSONResponse(self.public_task(self._task(request.path_params["task_id"])))

    async def task_trace(self, request: Request) -> Response:
        task = self._task(request.path_params["task_id"])
        task_id = task["task_id"]
        turns = self.store.all("SELECT * FROM turns WHERE task_id = ? ORDER BY created_at", (task_id,))
        events = self.store.all("SELECT * FROM chat_events WHERE task_id = ? ORDER BY seq", (task_id,))
        tool_calls = self.store.all("SELECT tool_use_id, turn_id, kind, tool, args_summary, target, read_only, status, "
                                    "result_summary, started_at, ended_at, duration_ms, approval_id, approval_decision, "
                                    "decided_via, decided_at FROM tool_calls WHERE task_id = ? ORDER BY started_at", (task_id,))
        interactions = [self.engine.interactions.public(i) for i in
                        self.store.all("SELECT * FROM interactions WHERE task_id = ? ORDER BY created_at", (task_id,))]
        deliveries = self.store.all("SELECT delivery_id, kind, tier, slot, state, playback, attempt, created_at, updated_at "
                                    "FROM deliveries WHERE task_id = ? OR CASE WHEN json_valid(payload_json) "
                                    "THEN json_extract(payload_json, '$.origin_task_id') END = ? "
                                    "ORDER BY gseq", (task_id, task_id))
        usage = self.store.all("SELECT ts, turn_id, model, mode, effort, turn_kind, request_id, ttft_ms, stop_reason, "
                               "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, "
                               "web_search_requests, web_fetch_requests, cost_usd, input_transformations_json "
                               "FROM usage WHERE task_id = ? ORDER BY id", (task_id,))
        transitions = self.store.all("SELECT from_state, to_state, reason, turn_id, ts FROM task_transitions "
                                     "WHERE task_id = ? ORDER BY id", (task_id,))
        created = timeutil.parse(task["created_at"])
        end = timeutil.parse(task["finished_at"]) if task["finished_at"] else timeutil.now()
        return JSONResponse(jsonlog.scrub({
            "task": self.public_task(task),
            "turns": [dict(self.public_turn(t), text_chars=len(t["text"] or "")) for t in turns],
            "events": [{"seq": e["seq"], "ts": e["ts"], "type": e["type"], "turn_id": e["turn_id"],
                        "data": json.loads(e["data_json"])} for e in events],
            "tool_calls": [dict(c, args_summary=loads(c["args_summary"])) for c in tool_calls],
            "interactions": interactions, "deliveries": deliveries, "transitions": transitions,
            "usage": [dict(u, input_transformations=loads(u.pop("input_transformations_json"))) for u in usage],
            "metrics": {"turns": len(turns), "interrupts": sum(1 for t in turns if t["state"] == "interrupted"),
                        "total_ms": int((end - created).total_seconds() * 1000), "cost_usd": task["cost_usd"],
                        "ttft_ms": [t["ttft_ms"] for t in turns]},
        }))

    # ------------------------------------------------------------------ outbox

    async def deliveries_stream(self, request: Request) -> Response:
        device_id = self._device_param(request)
        outbox = self.svc.outbox
        consumer = outbox.register_consumer(device_id)
        self.store.record_connection("outbox_open", device_id)

        async def gen():
            try:
                while outbox.is_current(device_id, consumer):
                    sent = False
                    for row in outbox.next_offers(device_id):
                        if not outbox.is_current(device_id, consumer):
                            break
                        offer = outbox.offer(row)
                        sent = True
                        yield f"id: {offer['gseq']}\nevent: delivery\ndata: {json.dumps(offer, ensure_ascii=False)}\n\n"
                    if not sent:
                        yield HEARTBEAT_FRAME
                    await outbox.wait(device_id, HEARTBEAT_S)
            finally:
                self.store.record_connection("outbox_close", device_id)

        return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)

    async def deliveries_ack(self, request: Request) -> Response:
        body = (await self._body(request, Ack)).model_dump(exclude_none=True)
        delivery_id = request.path_params["delivery_id"]
        try:
            public = self.svc.outbox.ack(delivery_id, body)
        except KeyError:
            raise ApiError(404, "not_found", "delivery not found")
        except StaleLease:
            raise ApiError(409, "stale_lease", "lease_id does not match the current offer")
        seen = body["outcome"] == "user_ack" or (body["outcome"] == "played" and public["payload"].get("seen") is True)
        if seen and public.get("chat_id"):
            self.svc.chats.mark_read(public["chat_id"])
        if body["outcome"] == "played" and (public.get("slot") or "").startswith("int:"):
            self.engine.interactions.open_voice_window(public["device_id"], public["slot"][4:])
        return JSONResponse(public)

    async def device_snapshot(self, request: Request) -> Response:
        device_id = request.path_params["device_id"]
        return JSONResponse({"device_id": device_id, "cards": self.svc.outbox.snapshot(device_id),
                             "chats": self.svc.chats.listing(device_id)})

    async def device_connection(self, request: Request) -> Response:
        device_id = request.path_params["device_id"]
        body = (await self._body(request, ConnectionEvent)).model_dump(exclude_none=True)
        self.store.device(device_id)
        values: Dict[str, Any] = {"last_seen_at": timeutil.iso(), "updated_at": timeutil.iso()}
        if body["kind"] in ("open", "close"):
            values["connected"] = int(body["kind"] == "open")
        else:
            values["busy"] = int(body["kind"] == "busy")
        self.store.update("devices", "device_id", device_id, values)
        self.store.record_connection(body["kind"], device_id, {k: v for k, v in body.items() if k != "kind"})
        jsonlog.log("device connection", device_id=device_id, event="connection", outcome=body["kind"])
        return JSONResponse({"device_id": device_id, "ok": True})
