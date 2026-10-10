"""ChatActor: one chat's turn in flight on the Anthropic tool runner (brief sections 6 and 9).

`drive()` runs one model loop: it builds the runner from the stored transcript, forwards stream events to
the turn's reply, appends every message itself (through `Transcript`) and handles the tool-round boundary,
where steers join, service notes are added and a switch to the task model starts a new runner (each model
has its own tool set). The runner runs in eager mode with every
call deferred: tools run only in `generate_tool_call_response()` after the stream ended, so a cancelled
stream ran nothing, and the runner reuses those results after `append_messages()` instead of running the
calls again. A cancelled reply is discarded and the uncommitted tail stays for the next request.
"""
import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Deque, Dict, List, Optional

import anthropic
from anthropic.lib.tools.mcp import async_mcp_tool

from . import app_tools, jsonlog, models
from .config import COMPACT_AT_TOKENS, MAX_ITERATIONS, PAUSE_RESUME_MAX, QUICK_MAX_ROUNDS, TURN_BUDGET_USD
from .memory import MemoryTool
from .transcript import Transcript, echo_form, without_unpaired_server_tools

if TYPE_CHECKING:
    from .turns import Engine, TurnRun

COMPACTION_INSTRUCTIONS = (
    "Summarize this conversation for continuing it. Keep: the open task and its progress, pending questions and "
    "approvals, Home Assistant calls whose result is unknown (never to be repeated), and every preference or fact "
    "the user stated. Write it as compact notes.")


async def close_runner(runner: Any, stream: Optional[Any]) -> None:
    """Close the HTTP stream of the request in flight, then the runner's generator, without raising."""
    if stream is not None:
        with contextlib.suppress(Exception):
            await stream.close()
    iterator = getattr(runner, "_iterator", None)
    if iterator is not None:
        with contextlib.suppress(BaseException):
            await iterator.aclose()


class Stop(Exception):
    """The loop ends without a model answer: code is an Error.code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Outcome:
    kind: str                      # end_turn | refusal | failed | max_turns | max_budget
    text: str = ""
    code: Optional[str] = None
    message: Optional[str] = None


@dataclass
class ChatActor:
    engine: "Engine"
    chat_id: str
    current: Optional["TurnRun"] = None
    queue: Deque[str] = field(default_factory=deque)
    dispatcher: Optional[asyncio.Task] = None
    loop: Optional[asyncio.Task] = None
    stopping: Optional[asyncio.Task] = None
    replies: Dict[str, Any] = field(default_factory=dict)
    stream: Optional[Any] = None
    streaming: bool = False
    resuming_pause: bool = False
    last_used: float = field(default_factory=time.monotonic)
    pending_uses: List[Dict[str, Any]] = field(default_factory=list)
    finishing: bool = False
    retry: bool = False
    _tools: Dict[str, Any] = field(default_factory=dict)

    @property
    def busy(self) -> bool:
        return self.current is not None or bool(self.queue)

    # ------------------------------------------------------------------ tools of this chat

    async def _resolve(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Identity of an HA call: the tool_use of the message being handled with this name and input."""
        run = self.current
        tool_use_id = None
        for use in self.pending_uses:
            if use["name"] == tool and use["input"] == args:
                tool_use_id = use["id"]
                self.pending_uses.remove(use)
                break
        chat = self.engine.store.chat(self.chat_id)
        return {"device_id": chat["device_id"] if chat else None, "chat_id": self.chat_id,
                "task_id": run.task_id if run else None, "turn_id": run.turn_id if run else None,
                "tool_use_id": tool_use_id}

    def tools(self, model: str, epoch: int, ha_specs: List[Dict[str, Any]]) -> List[Any]:
        """The request tools of this chat epoch on `model`; the same objects (and bytes) for every request."""
        key = f"{epoch}:{model}"
        if key not in self._tools:
            session = self.engine.ha.session(self._resolve)
            ha = [async_mcp_tool(tool, session, defer_loading=None if tool.name in models.HOT_HA_TOOLS else True)
                  for tool in self.engine.ha.mcp_tools(ha_specs)]
            tools: List[Any] = list(app_tools.build(self._app_tool)) + ha
            if model != models.MODELS[models.QUICK]:
                tools.append(MemoryTool(self.engine.settings.memory_dir, chat_id=self.chat_id))
            self._tools = {k: v for k, v in self._tools.items() if k.startswith(f"{epoch}:")}
            self._tools[key] = tools + models.server_tools(model)
        return self._tools[key]

    async def _app_tool(self, name: str, args: Dict[str, Any]) -> str:
        return await self.engine.app_tool(self, name, args)

    # ------------------------------------------------------------------ the model loop

    async def drive(self, run: "TurnRun", compose: bool = True) -> None:
        engine = self.engine
        transcript = Transcript(engine.store, self.chat_id)
        prompt = engine.prompt(self.chat_id)
        if compose:
            engine.compose_turn(run, transcript)
        chat = engine.store.chat(self.chat_id)
        model = models.model_for(run.mode, chat["task_model"])
        runner = self._runner(model, transcript, prompt)
        pauses = 0
        continued = False
        outcome: Optional[Outcome] = None
        try:
            requests = 0
            while True:
                if run.cost_usd >= TURN_BUDGET_USD:
                    raise Stop("max_budget", "turn budget reached")
                if requests >= MAX_ITERATIONS:
                    raise Stop("max_turns", "iteration limit reached")
                requests += 1
                sent_at = time.monotonic()
                run.sent_at = run.sent_at or sent_at
                run.heard = ""
                self.streaming = True
                try:
                    stream = await runner.__anext__()
                except StopAsyncIteration:
                    self.streaming = False
                    if outcome is None:
                        outcome = Outcome("max_turns", code="max_turns", message="iteration limit reached")
                    break
                self.stream = stream
                ttft = await self._forward(run, runner, stream, sent_at)
                message = await stream.get_final_message()
                self.stream = None
                self.streaming = False
                self.resuming_pause = False
                run = self.current or run
                engine.account(run, message, model, ttft, stream)
                stop = message.stop_reason
                if stop == "compaction":
                    transcript.new_epoch(as_param(message), run.turn_id, message.model, _request_id(stream))
                    engine.on_compaction(self.chat_id, run)
                    continue
                if stop == "refusal":
                    outcome = Outcome("refusal", code="refusal", message="the model declined")
                    continue
                assistant = as_param(message)
                assistant["content"] = echo_form(assistant["content"])
                if stop != "pause_turn":
                    assistant["content"] = without_unpaired_server_tools(assistant["content"])
                transcript.commit()
                transcript.append([assistant], run.turn_id, state="committed", model=message.model,
                                  request_id=_request_id(stream))
                engine.record_tool_uses(run, message)
                if stop == "tool_use":
                    run.rounds += 1
                    self.pending_uses = [{"id": b.id, "name": b.name, "input": b.input}
                                         for b in message.content if b.type == "tool_use"]
                    response = await runner.generate_tool_call_response()
                    self.pending_uses = []
                    run = self.current or run
                    engine.record_tool_results(run, response)
                    switch = run.mode == models.QUICK and (run.started_task or run.rounds >= QUICK_MAX_ROUNDS)
                    appended: List[Dict[str, Any]] = []
                    if switch:
                        appended.append(engine.switch_to_task(run))
                        model = models.model_for(models.TASK, engine.store.chat(self.chat_id)["task_model"])
                    blocks = list((response or {}).get("content") or [])
                    run, user_extra, notes = engine.boundary(self, run)
                    appended.append({"role": "user", "content": blocks + user_extra})
                    if notes:
                        appended.append(notes)
                    transcript.append(appended, run.turn_id)
                    if switch:
                        await close_runner(runner, None)
                        runner = self._runner(model, transcript, prompt)
                    else:
                        runner.append_messages(assistant, *appended)
                    continue
                if stop == "pause_turn":
                    pauses += 1
                    if pauses > PAUSE_RESUME_MAX:
                        outcome = Outcome("failed", code="api_error", message="paused too often")
                        break
                    self.resuming_pause = True
                    runner.append_messages(assistant)
                    continue
                if stop in ("max_tokens", "model_context_window_exceeded") and not continued:
                    continued = True
                    await close_runner(runner, None)
                    engine.continue_after_limit(run, transcript, stop)
                    runner = self._runner(model, transcript, prompt, compact=stop == "model_context_window_exceeded")
                    continue
                if stop in ("max_tokens", "model_context_window_exceeded"):
                    outcome = Outcome("failed", code="api_error", message=f"stopped on {stop}")
                    continue
                outcome = Outcome("end_turn", text="".join(b.text for b in message.content if b.type == "text"))
                if models.total_input(models.usage_numbers(message.usage)) > COMPACT_AT_TOKENS:
                    with contextlib.suppress(ValueError):
                        runner.compact_before_next_turn({"type": "summarize", "instructions": COMPACTION_INSTRUCTIONS})
                        jsonlog.log("compaction scheduled", chat_id=self.chat_id, turn_id=run.turn_id,
                                    event="compaction")
        except Stop as stop:
            outcome = Outcome("failed" if stop.code != "max_budget" else "max_budget", code=stop.code,
                              message=stop.message)
        except anthropic.APIError as e:
            outcome = engine.api_failure(run, e, model)
        finally:
            self.streaming = False
            self.resuming_pause = False
            self.pending_uses = []
            await asyncio.shield(close_runner(runner, self.stream))
            self.stream = None
        self.finishing = True
        await engine.finish_drive(self, self.current or run, outcome)

    def _runner(self, model: str, transcript: Transcript, prompt: Dict[str, Any], compact: bool = False) -> Any:
        engine = self.engine
        kwargs = models.request_kwargs(model, prompt["system_text"], engine.prefix_mismatch_behavior)
        runner = engine.client.beta.messages.tool_runner(
            messages=transcript.messages(), tools=self.tools(model, transcript.epoch, prompt["ha_specs"]),
            stream=True, max_iterations=MAX_ITERATIONS, run_tools_eagerly=True, **kwargs)
        if compact:
            runner.compact_before_next_turn({"type": "summarize", "instructions": COMPACTION_INSTRUCTIONS})
        return runner

    async def _forward(self, run: "TurnRun", runner: Any, stream: Any, sent_at: float) -> Optional[int]:
        """Stream events to the turn's reply and progress; returns the time to the first text delta (ms).

        Every client tool call is deferred, so none runs while the reply streams."""
        engine = self.engine
        ttft = None
        async for event in stream:
            run = self.current or run
            kind = event.type
            if kind == "message_start":
                transformations = getattr(event.message, "input_transformations", None)
                if transformations:
                    engine.transformations(run, transformations)
            elif kind == "content_block_start":
                block = event.content_block
                if block.type == "tool_use":
                    runner.defer_tool_call(block)
                if block.type in ("tool_use", "server_tool_use"):
                    engine.on_tool_start(run, block.name, server=block.type == "server_tool_use")
            elif kind == "content_block_delta":
                delta = event.delta
                if delta.type == "text_delta" and delta.text:
                    if ttft is None:
                        ttft = int((time.monotonic() - sent_at) * 1000)
                    engine.on_text(run, delta.text, ttft)
                elif delta.type == "thinking_delta" and getattr(delta, "thinking", ""):
                    engine.on_progress_note(run, delta.thinking)
            elif kind == "content_block_stop":
                engine.on_block_stop(run)
        return ttft


def as_param(message: Any) -> Dict[str, Any]:
    """A response message as a JSON message param, blocks exactly as the API sent them."""
    return {"role": message.role, "content": [block.to_dict(mode="json") for block in message.content]}


def _request_id(stream: Any) -> Optional[str]:
    with contextlib.suppress(Exception):
        return stream.request_id
    return None
