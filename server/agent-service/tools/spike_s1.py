"""S0/S1 gate of feat-agent-service (brief section 13): live measurements against the Anthropic API and ha-mcp.

Run from server/agent-service with the keys of server/.env in the environment (never printed):
  (set -a; . ../.env; set +a; .venv/bin/python tools/spike_s1.py <item> --out <dir>)
Items: tokens, ttft, cancel, steer, web, held, switch, all. Rows go to <out>/<item>.jsonl, request bodies of
each conversation to <out>/bodies/<conversation>.jsonl (input for the claude-api skill's prefix_diff.py).
Only read-only HA tools run, except the `held` item's single held call on a non-existent input_boolean,
which is denied through /api/policy/deny.
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

import anthropic  # noqa: E402
from anthropic.lib.tools.mcp import async_mcp_tool  # noqa: E402
from mcp.types import CallToolResult, TextContent  # noqa: E402

from weixue_agent import app_tools, jsonlog, models  # noqa: E402
from weixue_agent.actor import close_runner  # noqa: E402
from weixue_agent.memory import MemoryTool  # noqa: E402
from weixue_hamcp import HaMcpClient  # noqa: E402

PROMPT = (ROOT / "weixue_agent" / "prompts" / "system.md").read_text()
QUESTIONS = ["What time is it in Dublin?", "Is the living room light on?"]
HELD_ENTITY = "input_boolean.weixue_s1_probe"


def context_note(lang: str = "en") -> Dict[str, Any]:
    local = time.strftime("%Y-%m-%d %H:%M %a")
    return {"role": "system", "content": f"[context: local time {local} (Europe/Dublin); device sim-device-01; "
                                         f"reply language {lang}]"}


def median(values: List[float]) -> Optional[float]:
    values = [v for v in values if v is not None]
    return round(statistics.median(values)) if values else None


def p90(values: List[float]) -> Optional[float]:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    k = (len(values) - 1) * 0.9
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return round(values[lo] + (values[hi] - values[lo]) * (k - lo))


class ReadOnlySession:
    """ha-mcp for the spike: read-only tools run, anything else is refused without a request."""

    def __init__(self, client: HaMcpClient, read_only: set):
        self.client = client
        self.read_only = read_only
        self.calls: List[str] = []

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None, **_: Any) -> CallToolResult:
        self.calls.append(name)
        if name not in self.read_only:
            return CallToolResult(content=[TextContent(type="text", text="Not run in this test.")], isError=True)
        result = await self.client.call_tool(name, dict(arguments or {}), read_timeout_s=30)
        return CallToolResult.model_validate({k: v for k, v in result.items()
                                              if k in ("content", "structuredContent", "isError")})


class Spike:
    def __init__(self, out: Path):
        self.out = out
        (out / "bodies").mkdir(parents=True, exist_ok=True)
        self.conversation = "default"
        http = anthropic.DefaultAsyncHttpxClient(event_hooks={"request": [self._capture]})
        self.client = anthropic.AsyncAnthropic(http_client=http)
        self.ha = HaMcpClient(os.environ["HA_MCP_URL"])
        self.specs: List[Dict[str, Any]] = []
        self.read_only: set = set()
        self.memory_dir = Path(tempfile.mkdtemp(prefix="s1-memory-"))
        self.started_task: Optional[str] = None

    async def _capture(self, request: Any) -> None:
        if request.url.path != "/v1/messages":
            return
        with open(self.out / "bodies" / f"{self.conversation}.jsonl", "a") as f:
            f.write(request.content.decode() + "\n")

    def row(self, item: str, **values: Any) -> None:
        print(json.dumps({"item": item, **values}, ensure_ascii=False, default=str), flush=True)
        with open(self.out / f"{item}.jsonl", "a") as f:
            f.write(json.dumps(values, ensure_ascii=False, default=str) + "\n")

    async def load_tools(self) -> None:
        await self.ha.initialize()
        self.specs = await self.ha.list_tools()
        self.read_only = {t["name"] for t in self.specs if (t.get("annotations") or {}).get("readOnlyHint")}

    async def app_handle(self, name: str, args: Dict[str, Any]) -> str:
        if name == "start_task":
            self.started_task = str(args.get("title"))
            return "Task started. Continue the work now."
        if name == "ask_user":
            return "The user has not answered yet; end the turn. Their answer will arrive as a new message."
        return "OK."

    def tools(self, config: str, model: str) -> List[Any]:
        session = self.session = ReadOnlySession(self.ha, self.read_only)
        from weixue_agent.ha_adapter import HaGateway
        ha_tools = []
        memory = config.endswith("+memory") or model == models.MODELS[models.TASK]
        config = config.removesuffix("+memory")
        for tool in HaGateway.mcp_tools(None, self.specs):  # type: ignore[arg-type]
            hot = tool.name in models.HOT_HA_TOOLS
            if config == "hot" and not hot:
                continue
            defer = None if config == "hot" else (False if hot and config == "search" else True)
            ha_tools.append(async_mcp_tool(tool, session, defer_loading=defer or None))
        tools: List[Any] = list(app_tools.build(self.app_handle)) + ha_tools
        if memory:
            tools.append(MemoryTool(self.memory_dir))
        server = models.server_tools(model)
        if config == "hot":
            server = [t for t in server if not t["type"].startswith("tool_search")]
        return tools + server

    def runner(self, model: str, messages: List[Dict[str, Any]], tools: List[Any], binding: str = "drop_block"):
        kwargs = models.request_kwargs(model, PROMPT, binding)
        return self.client.beta.messages.tool_runner(messages=messages, tools=tools, stream=True,
                                                     max_iterations=12, run_tools_eagerly=True, **kwargs)

    async def drive(self, runner: Any, messages: List[Dict[str, Any]], t0: float, marks: Dict[str, Any],
                    cancel_on_text: bool = False) -> Dict[str, Any]:
        """Iterate a runner like the actor: forward events, append each message and tool results ourselves."""
        stats: Dict[str, Any] = {"requests": [], "tools": [], "server_tools": [], "stop_reasons": [], "text": ""}
        async for stream in runner:
            started = time.perf_counter()
            req: Dict[str, Any] = {}
            async for event in stream:
                now = time.perf_counter()
                if event.type == "message_start":
                    req.setdefault("first_event_ms", round((now - started) * 1000))
                    transformations = getattr(event.message, "input_transformations", None)
                    if transformations:
                        req["input_transformations"] = [t.to_dict() if hasattr(t, "to_dict") else t
                                                        for t in transformations]
                if event.type == "content_block_start":
                    kind = event.content_block.type
                    if kind == "tool_use":
                        runner.defer_tool_call(event.content_block)
                    if kind in ("tool_use", "server_tool_use"):
                        marks.setdefault("first_output", now)
                        marks.setdefault("first_" + kind, now)
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    marks.setdefault("first_output", now)
                    marks.setdefault("first_text", now)
                    req.setdefault("first_text_ms", round((now - started) * 1000))
                    if cancel_on_text:
                        return stats
            message = await stream.get_final_message()
            numbers = models.usage_numbers(message.usage)
            req.update(model=message.model, stop=message.stop_reason, **numbers,
                       request_id=getattr(stream, "request_id", None))
            if getattr(message, "input_transformations", None):
                req["input_transformations"] = [t.to_dict() for t in message.input_transformations]
            stats["requests"].append(req)
            stats["stop_reasons"].append(message.stop_reason)
            for block in message.content:
                if block.type == "tool_use":
                    stats["tools"].append(block.name)
                elif block.type == "server_tool_use":
                    stats["server_tools"].append(block.name)
                elif block.type == "text":
                    stats["text"] += block.text
            if message.stop_reason == "tool_use":
                response = await runner.generate_tool_call_response()
                appended = [message.to_param()] + ([response] if response else [])
                messages.extend(appended)
                runner.append_messages(*appended)
                if self.started_task and not stats.get("switched"):
                    stats["switched"] = True
                    switch = models.effort_message(models.EFFORT[models.TASK])
                    messages.append(switch)
                    runner.append_messages(switch)
                    runner.set_messages_params(lambda p: {**p, "model": models.MODELS[models.TASK]})
            elif message.stop_reason in ("pause_turn", "compaction"):
                messages.append(message.to_param())
                runner.append_messages(message.to_param())
            else:
                messages.append(message.to_param())
        stats["total_ms"] = round((time.perf_counter() - t0) * 1000)
        return stats

    # ------------------------------------------------------------------ items

    async def tokens(self) -> None:
        model = models.MODELS[models.QUICK]
        for config in ("search", "alldeferred", "hot", "full"):
            tools = self.tools("search" if config == "full" else config, model)
            dicts = [t.to_dict() if hasattr(t, "to_dict") else t for t in tools]
            if config == "full":
                dicts = [{k: v for k, v in d.items() if k != "defer_loading"} for d in dicts
                         if not str(d.get("type", "")).startswith("tool_search")]
            count = await self.client.beta.messages.count_tokens(
                model=model, system=[{"type": "text", "text": PROMPT}], tools=dicts,
                messages=[{"role": "user", "content": "Is the living room light on?"}], betas=models.BETAS)
            self.row("tokens", config=config, tools=len(dicts), input_tokens=count.input_tokens,
                     tool_json_bytes=len(json.dumps(dicts)))

    async def ttft(self, configs: List[str], runs: int) -> None:
        model = models.MODELS[models.QUICK]
        summary = []
        for config in configs:
            for q in QUESTIONS:
                rows = []
                for i in range(runs + 1):
                    self.conversation = f"ttft-{config}-{QUESTIONS.index(q)}-{i}"
                    tools = self.tools(config, model)
                    messages = [{"role": "user", "content": q}, context_note()]
                    marks: Dict[str, Any] = {}
                    t0 = time.perf_counter()
                    stats = await self.drive(self.runner(model, list(messages), tools), messages, t0, marks)
                    ms = {k: round((v - t0) * 1000) for k, v in marks.items()}
                    first = stats["requests"][0] if stats["requests"] else {}
                    row = {"config": config, "q": q, "run": i, "warmup": i == 0, **{f"{k}_ms": v for k, v in ms.items()},
                           "total_ms": stats["total_ms"], "requests": len(stats["requests"]),
                           "tools": stats["tools"], "server_tools": stats["server_tools"],
                           "ha_calls": list(self.session.calls),
                           "input_tokens": models.total_input(first) if first else None,
                           "cache_read": first.get("cache_read_tokens"), "text": stats["text"][:100]}
                    self.row("ttft", **row)
                    if i:
                        rows.append(row)
                summary.append({"config": config, "q": q,
                                "first_output_med": median([r.get("first_output_ms") for r in rows]),
                                "first_output_p90": p90([r.get("first_output_ms") for r in rows]),
                                "first_text_med": median([r.get("first_text_ms") for r in rows]),
                                "first_text_p90": p90([r.get("first_text_ms") for r in rows]),
                                "searches_per_turn": statistics.mean(
                                    r["server_tools"].count("tool_search_tool_bm25") for r in rows),
                                "input_tokens": rows[-1]["input_tokens"] if rows else None})
        for s in summary:
            self.row("ttft_summary", **s)

    async def cancel(self, runs: int) -> None:
        """Fable task mode: cancel the stream at its first text delta; time to a closed stream."""
        model = models.MODELS[models.TASK]
        times = []
        for i in range(runs):
            self.conversation = f"cancel-{i}"
            tools = self.tools("search", model)
            messages = [{"role": "user", "content": "Tell me a long story about a lighthouse keeper, at least 300 words."},
                        context_note(), models.effort_message("high")]
            marks: Dict[str, Any] = {}
            t0 = time.perf_counter()
            runner = self.runner(model, list(messages), tools)
            got_text = asyncio.Event()

            current: Dict[str, Any] = {}

            async def read():
                try:
                    async for stream in runner:
                        current["stream"] = stream
                        async for event in stream:
                            if event.type == "content_block_delta" and event.delta.type == "text_delta":
                                marks.setdefault("first_text", time.perf_counter())
                                got_text.set()
                finally:
                    await asyncio.shield(close_runner(runner, current.get("stream")))

            task = asyncio.create_task(read())
            await asyncio.wait_for(got_text.wait(), timeout=120)
            c0 = time.perf_counter()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            closed = current["stream"].response.is_closed
            stop_ms = round((time.perf_counter() - c0) * 1000)
            times.append(stop_ms)
            self.row("cancel", run=i, first_text_ms=round((marks["first_text"] - t0) * 1000), cancel_ms=stop_ms,
                     http_closed=closed)
        self.row("cancel_summary", cancel_med_ms=median(times), cancel_p90_ms=p90(times), runs=runs)

    async def steer(self) -> None:
        """Fable, binding 'error': a steer at a tool boundary, then a cancel and the one permitted tail rewrite."""
        model = models.MODELS[models.TASK]
        self.conversation = "steer"
        tools = self.tools("search", model)
        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": "Check the state of light.living_room_ceiling_lights for me."},
            context_note(), models.effort_message("high")]
        runner = self.runner(model, list(messages), tools, binding="error")
        steered = False
        results = []
        async for stream in runner:
            async for _ in stream:
                pass
            message = await stream.get_final_message()
            results.append({"stop": message.stop_reason, "transformations":
                            [t.to_dict() for t in (getattr(message, "input_transformations", None) or [])],
                            "text": "".join(b.text for b in message.content if b.type == "text")[:160]})
            if message.stop_reason == "tool_use":
                response = await runner.generate_tool_call_response()
                content = list(response["content"])
                appended: List[Dict[str, Any]] = [message.to_param()]
                if not steered:
                    steered = True
                    content.append({"type": "text", "text": "Actually, also tell me what time it is."})
                    appended.append({"role": "user", "content": content})
                    appended.append({"role": "system", "content": "[note: the user spoke while you were working on "
                                     "\"check the light\"; answer them, then continue, change course or stop as they ask]"})
                else:
                    appended.append({"role": "user", "content": content})
                messages.extend(appended)
                runner.append_messages(*appended)
            else:
                messages.append(message.to_param())
        self.row("steer", step="boundary_steer", requests=results)
        # Turn 2: sent, then cancelled at the first text delta (the tail stays uncommitted).
        tail = {"role": "user", "content": [{"type": "text", "text": "Now tell me a long story about that light."}]}
        sent = messages + [tail, context_note()]
        runner = self.runner(model, list(sent), tools, binding="error")
        heard = ""

        async def read():
            nonlocal heard
            async for stream in runner:
                async for event in stream:
                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        heard += event.delta.text
                        if len(heard) > 40:
                            return

        await read()
        await close_runner(runner, None)
        rewritten = {"role": "user", "content": tail["content"] + [{"type": "text", "text": "Actually, make it short."}]}
        notes = {"role": "system", "content": f'[note: your previous reply was cut off; the user heard: "{heard.strip()}"]'}
        final = messages + [rewritten, notes]
        runner = self.runner(model, list(final), tools, binding="error")
        out = []
        async for stream in runner:
            async for _ in stream:
                pass
            message = await stream.get_final_message()
            out.append({"stop": message.stop_reason, "transformations":
                        [t.to_dict() for t in (getattr(message, "input_transformations", None) or [])],
                        "text": "".join(b.text for b in message.content if b.type == "text")[:160]})
        self.row("steer", step="tail_rewrite_after_cancel", heard=heard[:80], requests=out)

    async def web(self, tries: int, model_mode: str) -> None:
        model = models.MODELS[model_mode]
        prompts = [
            "Search the web separately for today's opening hours of the National Gallery of Ireland, the Little Museum "
            "of Dublin, EPIC The Irish Emigration Museum, the Chester Beatty and the National Museum of Ireland, then "
            "tell me which closes last.",
            "Look up the weather forecast for Dublin, Cork, Galway, Limerick and Belfast for tomorrow, one search each, "
            "and tell me where it will be warmest.",
        ]
        for i in range(tries):
            self.conversation = f"web-{model_mode}-{i}"
            tools = self.tools("search", model)
            messages: List[Dict[str, Any]] = [{"role": "user", "content": prompts[i % len(prompts)]}, context_note()]
            if model_mode == models.TASK:
                messages.append(models.effort_message("high"))
            t0 = time.perf_counter()
            try:
                stats = await self.drive(self.runner(model, list(messages), tools), messages, t0, {})
            except anthropic.BadRequestError as e:
                self.row("web", model=model, run=i, error=jsonlog.truncate(str(e), 300))
                return
            paused = stats["stop_reasons"].count("pause_turn")
            paused_in_transcript = sum(1 for m in messages if m["role"] == "assistant") if paused else 0
            self.row("web", model=model, run=i, stop_reasons=stats["stop_reasons"], pause_turns=paused,
                     server_tools=stats["server_tools"], total_ms=stats["total_ms"],
                     assistant_messages=paused_in_transcript, text=stats["text"][:120])
            if paused:
                return

    async def held(self) -> None:
        """One ADR-9 held call through the real HaGateway: pending at the boundary, owned, denied."""
        from weixue_agent.config import load_settings
        from weixue_agent.ha_adapter import HaGateway
        from weixue_agent.store import Store
        data = Path(tempfile.mkdtemp(prefix="s1-held-"))
        settings = load_settings({"AGENT_SERVICE_TOKEN": "s" * 40, "HA_MCP_URL": os.environ["HA_MCP_URL"],
                                  "AGENT_DATA_DIR": str(data)})
        store = Store(data / "agent.db")
        gateway = HaGateway(settings, store, HaMcpClient(os.environ["HA_MCP_URL"]))
        await gateway.refresh()
        args = {"domain": "input_boolean", "service": "toggle", "entity_id": HELD_ENTITY}
        waiter = {"device_id": "sim-device-01", "chat_id": "cht_S1", "task_id": None, "turn_id": "trn_S1",
                  "tool_use_id": "toolu_s1"}
        t0 = time.perf_counter()
        result = await gateway.call(waiter, "ha_call_service", args)
        boundary_ms = round((time.perf_counter() - t0) * 1000)
        text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
        row = store.one("SELECT approval_id, state FROM approvals")
        self.row("held", step="call", boundary_ms=boundary_ms, pending=text.startswith("PENDING_APPROVAL"),
                 state=row["state"])
        owned = None
        for _ in range(10):
            await gateway.poll_once()
            owned = store.one("SELECT ha_token IS NOT NULL AS owned, ha_expires_at FROM approvals")
            if owned["owned"]:
                break
            await asyncio.sleep(1)
        self.row("held", step="correlate", owned=bool(owned["owned"]), expires_at=owned["ha_expires_at"])
        d0 = time.perf_counter()
        decided = await gateway.decide(row["approval_id"], "deny", "api", "sim-device-01")
        for _ in range(40):
            current = store.approval(row["approval_id"])
            if current["state"] in ("denied", "executed", "failed", "unknown", "expired"):
                break
            await asyncio.sleep(0.5)
        current = store.approval(row["approval_id"])
        pending = await gateway.client.pending()
        self.row("held", step="deny", decision_state=decided["state"], final_state=current["state"],
                 result=jsonlog.truncate(current["result_text"], 160), deny_to_final_ms=round((time.perf_counter() - d0) * 1000),
                 still_pending=sum(1 for p in pending if p.get("tool_name") == "ha_call_service"))
        await gateway.stop()

    async def switch(self) -> None:
        """quick (Sonnet) → start_task → task (Fable) → end → quick, in one append-only conversation."""
        self.conversation = "switch"
        self.started_task = None
        quick, task = models.MODELS[models.QUICK], models.MODELS[models.TASK]
        tools = self.tools("search", quick)
        messages: List[Dict[str, Any]] = [{"role": "user", "content": "Is the living room light on?"}, context_note()]
        log = []
        for step, (model, text) in enumerate([
            (quick, None),
            (quick, "Look at the living room lights' history today and draft (don't apply) an automation that turns "
                    "them on at sunset. Start a task for it."),
            (quick, "Thanks. What time is it now?"),
        ]):
            if text is not None:
                if messages[-1].get("output_config", {}).get("effort") != "low" and log and log[-1].get("switched"):
                    messages.append(models.effort_message("low"))
                messages.extend([{"role": "user", "content": text}, context_note()])
            self.started_task = None
            marks: Dict[str, Any] = {}
            t0 = time.perf_counter()
            stats = await self.drive(self.runner(model, list(messages), tools, binding="drop_block"), messages, t0,
                                     marks)
            log.append({"step": step, "switched": bool(stats.get("switched")), "requests": stats["requests"],
                        "tools": stats["tools"], "text": stats["text"][:120]})
            self.row("switch", **log[-1])

    async def close(self) -> None:
        await self.ha.aclose()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("item", choices=["tokens", "ttft", "cancel", "steer", "web", "held", "switch"])
    parser.add_argument("--out", required=True)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--configs", default="search,alldeferred,hot")
    parser.add_argument("--mode", default="quick")
    args = parser.parse_args()
    jsonlog.register_secrets([os.environ.get("ANTHROPIC_API_KEY"), os.environ.get("HA_MCP_URL")])
    spike = Spike(Path(args.out))
    try:
        if args.item != "held":
            await spike.load_tools()
        if args.item == "tokens":
            await spike.tokens()
        elif args.item == "ttft":
            await spike.ttft(args.configs.split(","), args.runs)
        elif args.item == "cancel":
            await spike.cancel(args.runs)
        elif args.item == "steer":
            await spike.steer()
        elif args.item == "web":
            await spike.web(args.runs, args.mode)
        elif args.item == "held":
            await spike.held()
        elif args.item == "switch":
            await spike.switch()
    finally:
        await spike.close()


if __name__ == "__main__":
    asyncio.run(main())
