"""In-process fake of ha-mcp 8.6.0: Streamable HTTP tools plus the policy HTTP API.

A tool listed in `held` blocks like a held call: it waits up to `wait_seconds` for a decision, then
returns USER_APPROVAL_REQUIRED with a token; approve runs it, deny returns USER_DENIED.
"""
import asyncio
import json
import secrets
import time
from typing import Any, Dict, List, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

TOOLS = [
    {"name": "ha_search", "description": "Search entities", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "ha_get_state", "description": "Get state", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"entity_id": {"type": "string"}}, "required": ["entity_id"]}},
    {"name": "ha_get_history", "description": "Get history", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"entity_id": {"type": "string"}}, "required": ["entity_id"]}},
    {"name": "ha_call_service", "description": "Call a service", "annotations": {"destructiveHint": True},
     "inputSchema": {"type": "object", "properties": {"domain": {"type": "string"}, "service": {"type": "string"},
                                                       "entity_id": {"type": "string"}}, "required": ["domain", "service"]}},
    {"name": "ha_config_set_automation", "description": "Create automation", "annotations": {"destructiveHint": True},
     "inputSchema": {"type": "object", "properties": {"config": {"type": "object"}}, "required": ["config"]},
     "outputSchema": {"type": "object"}},
]
RULES = [
    {"tool_name": "ha_call_service", "when": [{"path": "args.domain", "op": "not_in", "value": ["light", "notify"]}],
     "remember_minutes": 0},
] + [
    {"tool_name": "ha_config_set_automation", "when": [{"path": f"config.{a}.*.{k}", "op": "regex", "value": "homeassistant\\."}],
     "remember_minutes": 0}
    for a in ("actions", "action") for k in ("action", "service")
]


class Pending:
    def __init__(self, tool: str, args: Dict[str, Any]):
        self.token = secrets.token_urlsafe(24)
        self.tool = tool
        self.args = args
        self.created_at = time.time()
        self.decision = "pending"
        self.event = asyncio.Event()
        self.timed_out = False


class FakeHaMcp:
    def __init__(self, wait_seconds: float = 3.0, ttl_s: float = 600):
        self.wait_seconds = wait_seconds
        self.ttl_s = ttl_s
        self.queue: Dict[str, Pending] = {}
        self.calls: List[Dict[str, Any]] = []
        self.executed: List[Dict[str, Any]] = []
        self.decisions: List[Dict[str, str]] = []
        self.states: Dict[str, str] = {"light.living_room_ceiling_lights": "off", "sun.sun": "above_horizon"}
        self.down = False
        self.list_pending_hidden = False
        self.rules = RULES
        self.call_delay = 0.0

    def _held(self, tool: str, args: Dict[str, Any]) -> bool:
        from weixue_agent import policy_model
        return policy_model.held(self.rules, tool, args)

    def _run(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        self.executed.append({"tool": tool, "args": args})
        if tool == "ha_call_service" and args.get("entity_id") in self.states:
            self.states[args["entity_id"]] = "on" if args.get("service") == "turn_on" else "off"
            return {"content": [{"type": "text", "text": json.dumps({"success": True})}]}
        if tool == "ha_get_state":
            state = self.states.get(args.get("entity_id"))
            if state is None:
                return {"content": [{"type": "text", "text": "ENTITY_NOT_FOUND"}], "isError": True}
            return {"content": [{"type": "text", "text": json.dumps({"entity_id": args["entity_id"], "state": state})}]}
        if tool == "ha_search":
            return {"content": [{"type": "text", "text": json.dumps({"results": list(self.states)})}]}
        return {"content": [{"type": "text", "text": json.dumps({"success": True, "tool": tool})}]}

    def _consume_approved(self, tool: str, args: Dict[str, Any]) -> Optional[Pending]:
        for p in list(self.queue.values()):
            if p.tool == tool and p.args == args and p.decision == "approved" and p.timed_out:
                del self.queue[p.token]
                return p
        return None

    async def call_tool(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append({"tool": tool, "args": args})
        if self.call_delay:
            await asyncio.sleep(self.call_delay)
        if not self._held(tool, args) or self._consume_approved(tool, args):
            return self._run(tool, args)
        pending = Pending(tool, args)
        self.queue[pending.token] = pending
        try:
            await asyncio.wait_for(pending.event.wait(), timeout=self.wait_seconds)
        except asyncio.TimeoutError:
            pending.timed_out = True
            return {"content": [{"type": "text", "text": json.dumps({"success": False, "error": {
                "code": "USER_APPROVAL_REQUIRED", "message": "Re-call this tool with the same arguments after the user approves.",
                "context": {"token": pending.token, "expires_in_seconds": self.ttl_s}}})}], "isError": True}
        self.queue.pop(pending.token, None)
        if pending.decision == "approved":
            return self._run(tool, args)
        return {"content": [{"type": "text", "text": json.dumps({"success": False, "error": {"code": "USER_DENIED"}})}],
                "isError": True}

    async def rpc(self, request: Request) -> Response:
        if self.down:
            return Response(status_code=503)
        message = await request.json()
        method = message.get("method")
        if "id" not in message:
            return Response(status_code=202)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "ha-mcp", "version": "8.6.0"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "ping":
            result = {}
        elif method == "tools/call":
            result = await self.call_tool(message["params"]["name"], message["params"].get("arguments") or {})
        else:
            return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "nope"}})
        return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": result})

    async def policy_pending(self, request: Request) -> Response:
        items = [] if self.list_pending_hidden else [
            {"token": p.token, "tool_name": p.tool, "args": {k: (v if isinstance(v, str) else json.dumps(v))[:512] if not isinstance(v, (int, float, bool)) else v
                                                              for k, v in p.args.items()},
             "created_at": p.created_at, "expires_at": p.created_at + self.ttl_s}
            for p in self.queue.values() if p.decision == "pending"]
        return JSONResponse({"pending": items})

    async def policy_config(self, request: Request) -> Response:
        return JSONResponse({"wait_seconds": self.wait_seconds, "approval_ttl_minutes": self.ttl_s / 60,
                             "version": 13, "rules": self.rules})

    async def _decide(self, request: Request, decision: str) -> Response:
        body = await request.json()
        pending = self.queue.get(body.get("token"))
        if pending is None:
            return JSONResponse({"error": "unknown token"}, status_code=404)
        if pending.decision != "pending":
            return JSONResponse({"error": "already decided", "current_decision": pending.decision}, status_code=409)
        pending.decision = decision
        pending.event.set()
        self.decisions.append({"token": pending.token, "decision": decision})
        if decision == "denied" or not pending.timed_out:
            if decision == "denied":
                self.queue.pop(pending.token, None)
        return JSONResponse({"approved": True} if decision == "approved" else {"denied": True})

    async def approve(self, request: Request) -> Response:
        return await self._decide(request, "approved")

    async def deny(self, request: Request) -> Response:
        return await self._decide(request, "denied")

    def app(self, prefix: str = "/private_test") -> Starlette:
        return Starlette(routes=[
            Route(prefix, self.rpc, methods=["POST"]),
            Route(f"{prefix}/api/policy/pending", self.policy_pending),
            Route(f"{prefix}/api/policy/config", self.policy_config),
            Route(f"{prefix}/api/policy/approve", self.approve, methods=["POST"]),
            Route(f"{prefix}/api/policy/deny", self.deny, methods=["POST"]),
        ])
