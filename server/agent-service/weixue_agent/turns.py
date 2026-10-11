"""Turn engine: messages become turns of the chat's one append-only conversation (decision 36, ADR-11).

A task is the chat's open work across turns. A message while a turn runs steers it: it joins the running
loop at the next tool boundary, or cancels a reply that is still streaming after STEER_WAIT_S. Every
message stream ends with `reply_end`; later results go out through the outbox at the task's tier.
"""
import asyncio
import contextlib
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import anthropic

from . import activity, app_tools, jsonlog, models, routing, timeutil
from .actor import ChatActor, Outcome
from .chats import ChatIndex
from .config import (
    ACK_AFTER_S, ACTIVITY_MIN_INTERVAL_S, ACTOR_IDLE_S, BARGE_IN_RESUME_S, CANCEL_MAX_S, DEVICE_ACTION_WAIT_S,
    DEVICE_RESULT_MAX, DONE_SPEECH_TTL_S, IDEMPOTENCY_TTL_S,
    MAX_OPEN_TASKS_PER_DEVICE, NOTIFY_TTL_S, PROGRESS_MIN_INTERVAL_S, QUEUE_WAIT_MS, REPLY_HOLD_S, STEER_WAIT_S,
    STREAMS_MAX, SUMMARY_MAX, TEXT_COALESCE_MS, TITLE_MAX, TURN_MAX_S, VOICE_ORIGINS, Settings,
)
from .deliveries import Outbox, agent_payload
from .ha_adapter import BLOCKED_TEXT, GUARD_TEXT, UNKNOWN_TEXT, UNREACHABLE_TEXT, HaGateway, describe_call
from .memory import memory_index
from .ids import new_id, trace_id as new_trace_id
from .sse import EventHub, TextCoalescer
from .store import TERMINAL_STATES, Store, dumps, loads
from .transcript import Transcript, user_blocks

PROMPT_PATH = __import__("pathlib").Path(__file__).resolve().parent / "prompts" / "system.md"
FAIL_SPEAK = {
    "refusal": {"en": "Sorry, I can't help with that one.", "zh": "抱歉，这个我帮不了。"},
    "rate_limit": {"en": "I'm being rate limited right now; try again in a minute.", "zh": "现在请求太多了，过一分钟再试。"},
    "auth": {"en": "My connection to Claude isn't authorised right now.", "zh": "我连不上 Claude，授权有问题。"},
    "billing": {"en": "My Claude account has a billing problem.", "zh": "我的 Claude 账户有账单问题。"},
    "timeout": {"en": "That took too long, so I stopped.", "zh": "这件事太久了，我先停下了。"},
    "service_restart": {"en": "I restarted while working on that, so it may not have finished.",
                        "zh": "我刚才重启了，那件事可能没做完。"},
    "max_budget": {"en": "That was getting too expensive, so I stopped.", "zh": "这件事花费太高，我先停下了。"},
    "max_turns": {"en": "That took too many steps, so I stopped.", "zh": "步骤太多了，我先停下了。"},
    "busy": {"en": routing.BUSY["en"], "zh": routing.BUSY["zh"]},
    "default": {"en": "Sorry, something went wrong with that.", "zh": "抱歉，出了点问题。"},
}
NOTICE_TITLE = {"en": "Notice", "zh": "通知"}
DISPOSITION_STATUS = {"executed": "ok", "failed": "error", "not_sent": "not_run", "blocked": "not_run",
                      "denied": "denied", "expired": "expired", "pending": "pending", "unknown": "unknown"}
EFFECT_STATUS = {"ok": "confirmed", "error": "failed", "denied": "not_run", "expired": "not_run", "not_run": "not_run"}
WHY = {"service_restart": "the service restarted", "cancelled": "the user stopped this task",
       "steered": "the turn was interrupted", "barge_in": "the turn was interrupted", "timeout": "the turn timed out"}
STARTED_TEXT = ("Task started. Continue the work now; your final reply is delivered to the user when you finish. "
                "The memory tool is available from now on.")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: Any = None,
                 headers: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.status, self.code, self.message, self.detail, self.headers = status, code, message, detail, headers or {}


def body_hash(body: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass
class Reply:
    """The spoken part of a turn for one message stream."""
    reply_id: str
    opened_at: float
    steer: bool = False
    # The stream is the device's voice turn: its speech is heard on the device.
    voice: bool = False
    open: bool = True
    spoken_any: bool = False
    hold_started: bool = False
    reason: Optional[str] = None
    timers: List[asyncio.TimerHandle] = field(default_factory=list)
    coalescer: Optional[TextCoalescer] = None


@dataclass
class TurnRun:
    turn_id: str
    task_id: Optional[str]
    chat_id: str
    device_id: str
    trace_id: str
    kind: str
    lang: str
    mode: str = models.QUICK
    text: str = ""
    reply: Optional[Reply] = None
    sent_at: Optional[float] = None
    first_text_at: Optional[float] = None
    heard: str = ""
    late: List[str] = field(default_factory=list)
    step: int = 0
    rounds: int = 0
    last_progress: float = 0.0
    last_phase: Optional[str] = None
    last_activity: Optional[str] = None
    futures: Dict[str, asyncio.Future] = field(default_factory=dict)
    stop_reason: Optional[str] = None
    stop_by: Optional[str] = None
    stop_at: Optional[float] = None
    started_task: bool = False
    cost_usd: float = 0.0
    note_buffer: str = ""
    last_note: str = ""
    timers: List[asyncio.TimerHandle] = field(default_factory=list)


@dataclass
class Activity:
    """A task's activity label: the one sent last, the one wanted next, and the turn that set it."""
    run: TurnRun
    label: Optional[str] = None
    want: Optional[str] = None
    at: float = float("-inf")
    timer: Optional[asyncio.TimerHandle] = None


class Engine:
    def __init__(self, settings: Settings, store: Store, hub: EventHub, client: Any, ha: HaGateway,
                 outbox: Outbox, chats: ChatIndex):
        from .interactions import Interactions
        self.settings = settings
        self.store = store
        self.hub = hub
        self.client = client
        self.ha = ha
        self.outbox = outbox
        self.chats = chats
        self.actors: Dict[str, ChatActor] = {}
        self.interactions = Interactions(self)
        self.prefix_mismatch_behavior = "drop_block"
        self.system_text = PROMPT_PATH.read_text()
        self._tasks: Set[asyncio.Task] = set()
        self.closed = False
        self.last_api_error: Optional[str] = None
        self.last_api_error_at: float = 0.0
        self.last_model_ok: Optional[float] = None
        self.device_actions: Dict[str, Tuple[asyncio.Future, str]] = {}
        self.activities: Dict[str, Activity] = {}
        ha.listener = self.interactions
        chats.in_flight = lambda chat_id: self.turn_in_flight(chat_id)

    # ------------------------------------------------------------------ helpers

    def spawn(self, coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def rt(self, chat_id: str) -> ChatActor:
        actor = self.actors.get(chat_id)
        if actor is None:
            actor = self.actors[chat_id] = ChatActor(self, chat_id)
            jsonlog.log("actor loaded", chat_id=chat_id, event="actor_load")
        actor.last_used = time.monotonic()
        return actor

    def turn_in_flight(self, chat_id: str) -> bool:
        actor = self.actors.get(chat_id)
        return bool(actor and actor.current is not None)

    def current_run(self, chat_id: str) -> Optional[TurnRun]:
        actor = self.actors.get(chat_id)
        return actor.current if actor else None

    def streams_in_flight(self) -> int:
        return sum(1 for a in self.actors.values() if a.current is not None)

    def has_pending(self, chat_id: str) -> bool:
        actor = self.actors.get(chat_id)
        if actor and actor.busy:
            return True
        return bool(self.store.scalar(
            "SELECT COUNT(*) FROM interactions WHERE chat_id = ? AND state IN ('pending', 'uncorrelated')", (chat_id,)))

    def reap_idle(self, idle_s: float = ACTOR_IDLE_S) -> int:
        dropped = 0
        for chat_id, actor in list(self.actors.items()):
            if not actor.busy and not self.has_pending(chat_id) and time.monotonic() - actor.last_used > idle_s:
                del self.actors[chat_id]
                dropped += 1
                jsonlog.log("actor evicted", chat_id=chat_id, event="actor_evict")
        return dropped

    def log(self, msg: str, run: Optional[TurnRun] = None, **fields: Any) -> None:
        if run is not None:
            fields.setdefault("task_id", run.task_id)
            fields.setdefault("chat_id", run.chat_id)
            fields.setdefault("turn_id", run.turn_id)
            fields.setdefault("device_id", run.device_id)
            fields.setdefault("trace_id", run.trace_id)
            fields.setdefault("mode", run.mode)
        jsonlog.log(msg, **fields)

    def emit(self, chat_id: str, type_: str, data: Dict[str, Any], trace_id: str, task_id: Optional[str] = None,
             turn_id: Optional[str] = None) -> Dict[str, Any]:
        return self.hub.append(chat_id, type_, data, trace_id, task_id=task_id, turn_id=turn_id)

    def context_note(self, device_id: str, lang: str) -> str:
        local = timeutil.now().astimezone(ZoneInfo(self.settings.tz))
        device = self.store.device(device_id)
        state = "busy" if device.get("busy") else "idle"
        index = memory_index(self.settings.memory_dir)
        return (f"[context: local time {local:%Y-%m-%d %H:%M %a} ({self.settings.tz}); device {device_id} ({state}); "
                f"reply language {lang}" + (f"; memory: {index}" if index else "") + "]")

    def set_task_state(self, task_id: str, state: str, reason: str, turn_id: Optional[str] = None,
                       **values: Any) -> None:
        task = self.store.task(task_id)
        if task is None or task["state"] in TERMINAL_STATES:
            return
        now = timeutil.iso()
        if task["state"] != state:
            self.store.insert("task_transitions", {"task_id": task_id, "turn_id": turn_id, "from_state": task["state"],
                                                   "to_state": state, "reason": reason, "ts": now})
            jsonlog.log("task state", task_id=task_id, chat_id=task["chat_id"], trace_id=task["trace_id"],
                        turn_id=turn_id, event="task_state", outcome=state, detail=reason)
        self.store.update("tasks", "task_id", task_id, {"state": state, "updated_at": now, **values})

    def open_task(self, chat_id: str) -> Optional[Dict[str, Any]]:
        chat = self.store.chat(chat_id)
        task = self.store.task(chat["open_task_id"]) if chat and chat["open_task_id"] else None
        return task if task is not None and task["state"] not in TERMINAL_STATES else None

    # ------------------------------------------------------------------ public API: messages

    async def handle_message(self, chat_ref: str, body: Dict[str, Any], key: str,
                             trace: Optional[str]) -> Dict[str, Any]:
        """Route one user message; returns the accepted response plus the stream bounds."""
        digest = body_hash(body)
        existing = self.store.one("SELECT * FROM idempotency WHERE key = ?", (key,))
        if existing:
            created = timeutil.parse(existing["created_at"])
            if (timeutil.now() - created).total_seconds() < IDEMPOTENCY_TTL_S:
                if existing["request_hash"] != digest or existing["scope"] != "message":
                    raise ApiError(409, "idempotency_mismatch", "Idempotency-Key was used with a different body")
                return dict(loads(existing["response_json"], {}), duplicate=True)
            self.store.execute("DELETE FROM idempotency WHERE key = ?", (key,))
        if not self.settings.anthropic_key_present:
            raise ApiError(503, "unavailable", "the Anthropic API key is not configured")
        device_id = body["device_id"]
        self.store.device(device_id)
        if chat_ref == "current":
            chat = self.chats.current(device_id)
        else:
            chat = self.store.chat(chat_ref)
            if chat is None or chat["device_id"] != device_id:
                raise ApiError(404, "not_found", "chat not found for this device")
        text = jsonlog.redact_text(body["text"])
        lang = routing.detect_lang(text, body.get("lang", "auto"))
        trace = trace or new_trace_id()
        reply_id = new_id("rpl")
        voice = (body.get("origin") or {}).get("kind") in VOICE_ORIGINS

        if voice:
            spoken = await self.interactions.route_spoken(device_id, chat, text, lang, trace, reply_id)
            if spoken is not None:
                return self._remember(key, digest, spoken)

        actor = self.rt(chat["chat_id"])
        open_task = self.open_task(chat["chat_id"])
        if routing.is_stop(text) and (open_task is not None or actor.current is not None):
            response = await self._voice_stop(chat, open_task, lang, trace, reply_id)
            return self._remember(key, digest, response)

        if open_task is not None and actor.current is None and not body.get("retry_of"):
            if voice:
                answered = await self.interactions.answer_by_message(open_task, text, lang, trace, reply_id)
                if answered is not None:
                    return self._remember(key, digest, answered)
            self.interactions.drop_questions(open_task)

        if body.get("retry_of"):
            original = self.store.task(body["retry_of"])
            if original is None or original["device_id"] != device_id:
                raise ApiError(404, "not_found", "task to retry not found")
            if not original["retry_allowed"] or original["state"] not in ("failed", "cancelled"):
                raise ApiError(409, "invalid_state", "this task cannot be retried")

        if actor.current is not None:
            routed = "steer"
        elif open_task is not None:
            routed = "continue"
        else:
            routed = "new_task"
        if routed == "new_task":
            open_count = self.store.scalar(
                "SELECT COUNT(*) FROM tasks WHERE device_id = ? AND state NOT IN ('done', 'failed', 'cancelled')",
                (device_id,))
            if open_count >= MAX_OPEN_TASKS_PER_DEVICE:
                raise ApiError(429, "too_many_tasks", "too many open tasks for this device", headers={"Retry-After": "30"})
        if actor.current is None:
            await self._reserve_stream(lang)

        response = self._enqueue(chat, open_task, routed, text, lang, trace, key, reply_id,
                                 retry_of=body.get("retry_of"), voice=voice)
        return self._remember(key, digest, response)

    def _enqueue(self, chat: Dict[str, Any], open_task: Optional[Dict[str, Any]], routed: str, text: str, lang: str,
                 trace: str, key: Optional[str], reply_id: Optional[str], retry_of: Optional[str] = None,
                 accepted_routed: Optional[str] = None, kind: Optional[str] = None,
                 voice: bool = False) -> Dict[str, Any]:
        """Persist the turn of a message (a second steer joins the unsent one), emit `accepted`, dispatch."""
        actor = self.rt(chat["chat_id"])
        device_id = chat["device_id"]
        merged = None
        if routed == "steer" and actor.queue:
            merged = self.store.turn(actor.queue[-1])
            previous = actor.replies.get(merged["turn_id"]) if merged else None
            if previous is not None:
                self.end_reply(chat["chat_id"], merged["turn_id"], previous, "gateway_closed", merged["trace_id"],
                               merged["task_id"])
        task_id = open_task["task_id"] if open_task else (actor.current.task_id if actor.current else None)
        now = timeutil.iso()
        with self.store.tx():
            if routed == "new_task" or task_id is None:
                routed = "new_task"
                task_id = new_id("tsk")
                self.store.insert("tasks", {
                    "task_id": task_id, "chat_id": chat["chat_id"], "device_id": device_id, "state": "progress",
                    "title": jsonlog.truncate(text, TITLE_MAX), "lang": lang, "retry_of": retry_of,
                    "trace_id": trace, "created_at": now, "updated_at": now,
                })
                self.store.insert("task_transitions", {"task_id": task_id, "from_state": None, "to_state": "progress",
                                                       "reason": "created", "ts": now})
                self.store.update("chats", "chat_id", chat["chat_id"], {"open_task_id": task_id})
            if merged is not None:
                turn_id = merged["turn_id"]
                self.store.update("turns", "turn_id", turn_id, {"text": merged["text"] + "\n" + text})
            else:
                turn_id = new_id("trn")
                self.store.insert("turns", {
                    "turn_id": turn_id, "task_id": task_id, "chat_id": chat["chat_id"],
                    "kind": kind or ("user" if routed == "new_task" else "steer"), "state": "queued", "routed": routed,
                    "text": text, "lang": lang, "trace_id": trace, "idempotency_key": key, "created_at": now,
                })
        task = self.store.task(task_id)
        if routed in ("steer", "continue"):
            self.set_task_state(task_id, "progress", routed, turn_id=turn_id)
            self.outbox.withdraw(device_id, f"task:{task_id}", spoken_only=True)
        shown = accepted_routed or routed
        accepted = self.emit(chat["chat_id"], "accepted", {"routed": shown, "task_state": "progress",
                                                           "reply_id": reply_id},
                             trace, task_id=task_id, turn_id=turn_id)
        if reply_id is not None:
            reply = Reply(reply_id=reply_id, opened_at=time.monotonic(), steer=routed != "new_task", voice=voice)
            actor.replies[turn_id] = reply
            self._arm_ack(chat["chat_id"], turn_id, reply, lang, trace, task_id)
        self.chats.touch(chat["chat_id"], first_text=text)
        self.log("message accepted", task_id=task_id, chat_id=chat["chat_id"], turn_id=turn_id, device_id=device_id,
                 trace_id=trace, event="accepted", outcome=shown)
        if merged is None:
            actor.queue.append(turn_id)
        if routed == "steer" and actor.current is not None:
            self._on_steer(actor, turn_id)
        self._ensure_dispatcher(actor)
        return {"chat_id": chat["chat_id"], "task_id": task_id, "turn_id": turn_id, "routed": shown,
                "reply_id": reply_id, "after": accepted["seq"] - 1, "duplicate": False,
                "trace_id": trace, "task_state": task["state"]}

    async def answer_turn(self, task: Dict[str, Any], text: str, lang: str, trace: str,
                          reply_id: Optional[str], voice: bool = False) -> Dict[str, Any]:
        """A late answer to a question: a turn of the waiting task in the same conversation."""
        chat = self.store.chat(task["chat_id"])
        actor = self.rt(chat["chat_id"])
        routed = "steer" if actor.current is not None else "continue"
        return self._enqueue(chat, task, routed, text, lang, trace, None, reply_id, accepted_routed="answer",
                             voice=voice)

    def _remember(self, key: str, digest: str, response: Dict[str, Any]) -> Dict[str, Any]:
        self.store.insert("idempotency", {"key": key, "scope": "message", "request_hash": digest,
                                          "ref_id": response.get("turn_id") or response.get("task_id") or "",
                                          "response_json": dumps(response), "created_at": timeutil.iso()},
                          or_ignore=True)
        return response

    async def _reserve_stream(self, lang: str) -> None:
        deadline = time.monotonic() + QUEUE_WAIT_MS / 1000
        while self.streams_in_flight() >= STREAMS_MAX:
            if time.monotonic() >= deadline:
                raise ApiError(503, "unavailable", "busy", {"reason": "busy", "speak": routing.BUSY[lang]})
            await asyncio.sleep(0.05)

    def stream_bounds(self, response: Dict[str, Any]):
        reply_id = response.get("reply_id")

        def until(event: Dict[str, Any]) -> bool:
            return event["type"] == "reply_end" and event["data"].get("reply_id") == reply_id

        return until, response.get("task_id")

    # ------------------------------------------------------------------ reply mechanics

    def reply_heartbeat(self, chat_id: str) -> bool:
        actor = self.actors.get(chat_id)
        return bool(actor and any(r.open for r in actor.replies.values()))

    def _arm_ack(self, chat_id: str, turn_id: str, reply: Reply, lang: str, trace: str, task_id: Optional[str]) -> None:
        loop = asyncio.get_running_loop()
        text = (routing.STEER_ACK if reply.steer else routing.ACK)[lang]
        reply.timers.append(loop.call_later(ACK_AFTER_S, self._ack_elapsed, chat_id, turn_id, reply, text, trace, task_id))

    def _ack_elapsed(self, chat_id: str, turn_id: str, reply: Reply, text: str, trace: str,
                     task_id: Optional[str]) -> None:
        if not reply.open or reply.spoken_any:
            return
        self.emit(chat_id, "text", {"delta": text}, trace, task_id=task_id, turn_id=turn_id)
        reply.spoken_any = True
        self.end_reply(chat_id, turn_id, reply, "ack", trace, task_id, ack_text=text)

    def end_reply(self, chat_id: str, turn_id: Optional[str], reply: Reply, reason: str, trace: str,
                  task_id: Optional[str], ack_text: Optional[str] = None, flush: bool = True) -> None:
        """End the open reply; text still buffered is spoken (flush) or dropped (a stopped turn)."""
        if not reply.open:
            return
        if reply.coalescer:
            if flush:
                reply.coalescer.flush()
            else:
                reply.coalescer.discard()
        reply.open = False
        reply.reason = reason
        for timer in reply.timers:
            timer.cancel()
        latency = int((time.monotonic() - reply.opened_at) * 1000)
        self.emit(chat_id, "reply_end", {"reason": reason, "ack_text": ack_text, "reply_id": reply.reply_id},
                  trace, task_id=task_id, turn_id=turn_id)
        if turn_id:
            self.store.update("turns", "turn_id", turn_id, {"reply_end_at": timeutil.iso(), "reply_end_reason": reason})
        jsonlog.log("reply end", chat_id=chat_id, turn_id=turn_id, task_id=task_id, trace_id=trace, event="reply_end",
                    outcome=reason, latency_ms=latency)
        actor = self.actors.get(chat_id)
        run = actor.current if actor else None
        if run is not None and run.task_id and run.turn_id == turn_id and run.last_phase is not None and \
                reason != "turn_done":
            last = loads((self.store.task(run.task_id) or {}).get("progress_json")) or {}
            self.progress(run, run.last_phase, tool=last.get("tool"), label=last.get("label"), force=True)

    def gateway_closed(self, chat_id: str, reply_id: str) -> None:
        """The provider's message stream dropped before reply_end (barge-in or disconnect)."""
        actor = self.actors.get(chat_id)
        if actor is None:
            return
        for turn_id, reply in list(actor.replies.items()):
            if reply.reply_id != reply_id or not reply.open:
                continue
            turn = self.store.turn(turn_id)
            trace = turn["trace_id"] if turn else new_trace_id()
            run = actor.current
            if run is not None and run.turn_id == turn_id and actor.streaming and run.heard and run.stop_reason is None:
                self.end_reply(chat_id, turn_id, reply, "gateway_closed", trace, run.task_id, flush=False)
                self.log("barge-in", run, event="barge_in", detail=jsonlog.truncate(run.heard, 60))
                self._stop(actor, "barge_in", None)
                return
            if reply.spoken_any and run is not None and run.turn_id == turn_id and run.heard:
                self.chats.add_note(chat_id, "note", f'[note: your last reply was cut off; the user heard: '
                                                     f'"{jsonlog.truncate(run.heard, 300)}"]')
            self.end_reply(chat_id, turn_id, reply, "gateway_closed", trace, turn["task_id"] if turn else None)

    def _reply_text(self, run: TurnRun, text: str) -> None:
        reply = run.reply
        if reply is None or not reply.open:
            run.late.append(text)
            return
        self.emit(run.chat_id, "text", {"delta": text}, run.trace_id, task_id=run.task_id, turn_id=run.turn_id)

    def speak_inline(self, run: TurnRun, text: str, reason: str) -> bool:
        """Speak `text` in the open reply and end it; False when no reply is open."""
        reply = run.reply
        if reply is None or not reply.open:
            return False
        if reply.coalescer:
            reply.coalescer.flush()
        self.emit(run.chat_id, "text", {"delta": text}, run.trace_id, task_id=run.task_id, turn_id=run.turn_id)
        reply.spoken_any = True
        self.end_reply(run.chat_id, run.turn_id, reply, reason, run.trace_id, run.task_id)
        return True

    def reply_open(self, run: Optional[TurnRun]) -> bool:
        return bool(run and run.reply and run.reply.open)

    def attach_reply(self, run: TurnRun, reply: Reply) -> None:
        """A message answered a question while its turn waits: the turn speaks again on the new stream."""
        actor = self.rt(run.chat_id)
        actor.replies[run.turn_id] = reply
        run.reply = reply
        run.heard = ""
        reply.coalescer = TextCoalescer(lambda text: self._reply_text(run, text), TEXT_COALESCE_MS / 1000)
        self._arm_ack(run.chat_id, run.turn_id, reply, run.lang, run.trace_id, run.task_id)

    # ------------------------------------------------------------------ dispatch

    def _ensure_dispatcher(self, actor: ChatActor) -> None:
        if self.closed:
            return
        if actor.dispatcher is None or actor.dispatcher.done():
            actor.dispatcher = self.spawn(self._dispatch(actor), f"dispatch:{actor.chat_id}")

    async def _dispatch(self, actor: ChatActor) -> None:
        while True:
            if actor.stopping is not None:
                with contextlib.suppress(BaseException):
                    await actor.stopping
                actor.stopping = None
            if not actor.queue:
                if not self._queue_event_turn(actor):
                    return
            turn_id = actor.queue.popleft()
            turn = self.store.turn(turn_id)
            if turn is None or turn["state"] != "queued":
                continue
            await self._run_turn(actor, turn)

    def _queue_event_turn(self, actor: ChatActor) -> bool:
        """Event lines waiting for the chat start an event turn of the open task."""
        if not self.chats.pending_notes(actor.chat_id, kind="event"):
            return False
        task = self.open_task(actor.chat_id) or self._event_task(actor.chat_id)
        turn_id = new_id("trn")
        self.store.insert("turns", {
            "turn_id": turn_id, "task_id": task["task_id"], "chat_id": actor.chat_id, "kind": "event",
            "state": "queued", "routed": None, "text": "", "lang": task["lang"], "trace_id": task["trace_id"],
            "created_at": timeutil.iso()})
        self.set_task_state(task["task_id"], "progress", "event turn", turn_id=turn_id)
        actor.queue.append(turn_id)
        return True

    def _event_task(self, chat_id: str) -> Dict[str, Any]:
        """A task for an outcome that arrives after the chat's work ended (e.g. a late HA result)."""
        chat = self.store.chat(chat_id)
        last = self.store.one("SELECT lang FROM tasks WHERE chat_id = ? ORDER BY created_at DESC LIMIT 1", (chat_id,))
        lang = last["lang"] if last else "en"
        now = timeutil.iso()
        task_id = new_id("tsk")
        trace = new_trace_id()
        self.store.insert("tasks", {
            "task_id": task_id, "chat_id": chat_id, "device_id": chat["device_id"], "state": "progress",
            "title": "Home Assistant update" if lang == "en" else "家居更新", "lang": lang, "trace_id": trace,
            "created_at": now, "updated_at": now})
        self.store.insert("task_transitions", {"task_id": task_id, "from_state": None, "to_state": "progress",
                                               "reason": "event", "ts": now})
        self.store.update("chats", "chat_id", chat_id, {"open_task_id": task_id})
        return self.store.task(task_id)

    def schedule_event_turn(self, chat_id: str) -> None:
        actor = self.rt(chat_id)
        if actor.current is None and not actor.queue:
            self._ensure_dispatcher(actor)

    def _new_run(self, actor: ChatActor, turn: Dict[str, Any]) -> TurnRun:
        chat = self.store.chat(actor.chat_id)
        task = self.store.task(turn["task_id"])
        reply = actor.replies.get(turn["turn_id"])
        run = TurnRun(turn_id=turn["turn_id"], task_id=turn["task_id"], chat_id=actor.chat_id,
                      device_id=chat["device_id"], trace_id=turn["trace_id"], kind=turn["kind"], lang=turn["lang"],
                      mode=(task or {}).get("mode") or models.QUICK, text=turn["text"] or "",
                      reply=reply if reply and reply.open else None)
        if run.reply is not None:
            run.reply.coalescer = TextCoalescer(lambda text: self._reply_text(run, text), TEXT_COALESCE_MS / 1000)
        return run

    def _start_turn(self, run: TurnRun, title: Optional[str]) -> None:
        chat = self.store.chat(run.chat_id)
        self.store.update("turns", "turn_id", run.turn_id, {"state": "running", "started_at": timeutil.iso(),
                                                           "epoch": chat["epoch"], "mode": run.mode})
        self.emit(run.chat_id, "started", {"kind": run.kind, "title": title}, run.trace_id, task_id=run.task_id,
                  turn_id=run.turn_id)
        self.log("turn start", run, event="turn_start", detail=run.kind, epoch=chat["epoch"])
        self.set_activity(run, activity.THINK)

    async def _run_turn(self, actor: ChatActor, turn: Dict[str, Any]) -> None:
        task = self.store.task(turn["task_id"])
        run = self._new_run(actor, turn)
        actor.current = run
        try:
            if task is None or task["state"] in TERMINAL_STATES:
                self.store.update("turns", "turn_id", run.turn_id, {"state": "failed", "terminal_reason": "skipped",
                                                                   "ended_at": timeutil.iso()})
                if run.reply:
                    self.end_reply(run.chat_id, run.turn_id, run.reply, "turn_done", run.trace_id, run.task_id)
                return
            self._start_turn(run, task["title"])
            timeout = asyncio.get_running_loop().call_later(TURN_MAX_S, self._turn_timeout, actor)
            compose = True
            try:
                while True:
                    actor.retry = False
                    actor.finishing = False
                    actor.loop = self.spawn(actor.drive(actor.current or run, compose), f"turn:{run.turn_id}")
                    await asyncio.wait({actor.loop})
                    if actor.loop.cancelled():
                        await self._stopped(actor, actor.current or run)
                    elif actor.loop.exception() is not None:
                        e = actor.loop.exception()
                        jsonlog.log("turn error", level="error", turn_id=run.turn_id, code=type(e).__name__,
                                    detail=jsonlog.truncate(str(e), 300))
                        await self._turn_failed(actor.current or run, "internal", "internal error")
                    if not actor.retry:
                        break
                    compose = False
            finally:
                timeout.cancel()
        except Exception as e:
            jsonlog.log("turn error", level="error", turn_id=run.turn_id, code=type(e).__name__,
                        detail=jsonlog.truncate(str(e), 300))
            await self._turn_failed(actor.current or run, "internal", "internal error")
        finally:
            final = actor.current or run
            for r in {id(run): run, id(final): final}.values():
                for timer in r.timers:
                    timer.cancel()
                actor.replies.pop(r.turn_id, None)
                if r.reply and r.reply.open:
                    self.end_reply(r.chat_id, r.turn_id, r.reply, "turn_done", r.trace_id, r.task_id,
                                   flush=r.stop_reason is None)
                self.set_activity(r, None)
            actor.current = None
            actor.loop = None
            actor.finishing = False
            actor.last_used = time.monotonic()

    # ------------------------------------------------------------------ transcript of a turn

    def prompt(self, chat_id: str) -> Dict[str, Any]:
        """System text and ha-mcp tool list frozen for the chat's current epoch."""
        chat = self.store.chat(chat_id)
        row = self.store.one("SELECT * FROM chat_prompts WHERE chat_id = ? AND epoch = ?", (chat_id, chat["epoch"]))
        if row is None:
            specs = self.ha.tools
            tools = {mode: [t["name"] for t in specs] for mode in (models.QUICK, models.TASK)}
            self.store.insert("chat_prompts", {"chat_id": chat_id, "epoch": chat["epoch"], "system_text": self.system_text,
                                               "ha_tools_json": dumps(specs), "tools_json": dumps(tools),
                                               "created_at": timeutil.iso()}, or_ignore=True)
            row = self.store.one("SELECT * FROM chat_prompts WHERE chat_id = ? AND epoch = ?", (chat_id, chat["epoch"]))
        return {"system_text": row["system_text"], "ha_specs": loads(row["ha_tools_json"], [])}

    def _unanswered_result(self, use: Dict[str, Any]) -> Dict[str, Any]:
        row = self.store.one("SELECT * FROM tool_calls WHERE tool_use_id = ?", (use.get("id"),))
        turn = self.store.turn(row["turn_id"]) if row else None
        why = WHY.get((turn or {}).get("terminal_reason") or "", "the turn ended")
        held = bool(row and not row["read_only"] and (
            (row["kind"] == "ha" and (row["approval_id"] or row["status"] in ("running", "pending", "unknown")))
            or (row["kind"] == "device" and row["status"] in ("dispatched", "unknown"))))
        if held:
            text = f"result unknown: {why}; do not repeat it"
            status = "unknown"
        elif row and row["status"] == "ok":
            text = f"completed; its result was not kept ({why})"
            status = "ok"
        else:
            text = f"not run: {why}"
            status = "not_run"
        if row and row["status"] in ("running", "dispatched"):
            self.store.update("tool_calls", "tool_use_id", row["tool_use_id"], {"status": status, "ended_at": timeutil.iso()})
        return {"type": "tool_result", "tool_use_id": use["id"], "content": text, "is_error": status != "ok"}

    def compose_turn(self, run: TurnRun, transcript: Transcript) -> None:
        """The turn's user message (open tool uses closed, then the user's words) and its system notes."""
        chat = self.store.chat(run.chat_id)
        blocks = [self._unanswered_result(use) for use in transcript.open_tool_uses()]
        if run.kind == "event":
            text = "\n".join(self.chats.take_notes(run.chat_id, run.turn_id, kind="event"))
        else:
            text = run.text
        if text:
            blocks.append({"type": "text", "text": text})
        notes = [self.context_note(run.device_id, run.lang)] + self.chats.take_notes(run.chat_id, run.turn_id)
        effort = models.EFFORT[run.mode]
        pre: List[Dict[str, Any]] = []
        if chat["effort"] != effort:
            pre.append(models.effort_message(effort))
            self.store.update("chats", "chat_id", run.chat_id, {"effort": effort, "mode": run.mode})
        tail = [loads(r["content_json"]) for r in transcript.tail()]
        if tail and any(m["role"] == "user" for m in tail):
            kept: List[Dict[str, Any]] = []
            user_content: List[Dict[str, Any]] = []
            tail_notes: List[Dict[str, Any]] = []
            for message in tail:
                if message["role"] == "user":
                    user_content.extend(user_blocks(message))
                elif message["role"] == "system" and message.get("content"):
                    tail_notes.extend(b for b in user_blocks(message) if not b.get("text", "").startswith("[context:"))
                else:
                    kept.append(message)
            answered = {b.get("tool_use_id") for b in user_content if b.get("type") == "tool_result"}
            new_blocks = [b for b in blocks if b.get("type") != "tool_result" or b.get("tool_use_id") not in answered]
            note_blocks = tail_notes + [{"type": "text", "text": n} for n in notes]
            messages = kept + pre + [{"role": "user", "content": user_content + new_blocks},
                                     {"role": "system", "content": note_blocks}]
            transcript.rewrite_tail(messages, run.turn_id)
            self.log("transcript tail rewritten", run, event="tail_rewrite")
            return
        if not blocks:
            blocks.append({"type": "text", "text": "[continue]"})
        transcript.append(pre + [{"role": "user", "content": blocks},
                                 {"role": "system", "content": [{"type": "text", "text": n} for n in notes]}],
                          run.turn_id)

    def boundary(self, actor: ChatActor, run: TurnRun) -> Tuple[TurnRun, List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """At a tool-round boundary: a queued steer joins (text after the tool results, then a separate system
        note), and pending service notes go into one system message."""
        extra: List[Dict[str, Any]] = []
        notes: List[str] = []
        steer = None
        for turn_id in list(actor.queue):
            turn = self.store.turn(turn_id)
            if turn and turn["state"] == "queued" and turn["kind"] == "steer":
                steer = turn
                break
        if steer is not None:
            task = self.store.task(run.task_id) if run.task_id else None
            title = (task or {}).get("title") or "your task"
            extra.append({"type": "text", "text": steer["text"]})
            notes.append(self.context_note(run.device_id, steer["lang"]))
            notes.append(f'[note: the user spoke while you were working on "{title}"; answer them, then continue, '
                         "change course or stop as they ask]")
            run = self._steer_handoff(actor, run, steer)
        notes.extend(self.chats.take_notes(run.chat_id, run.turn_id))
        system = {"role": "system", "content": [{"type": "text", "text": n} for n in notes]} if notes else None
        return run, extra, system

    def _steer_handoff(self, actor: ChatActor, run: TurnRun, steer: Dict[str, Any]) -> TurnRun:
        drain_ms = int((time.monotonic() - run.stop_at) * 1000) if run.stop_at else None
        now = timeutil.iso()
        self.store.update("turns", "turn_id", run.turn_id, {
            "state": "interrupted", "terminal_reason": "steered", "ended_at": now, "interrupted_by": steer["turn_id"],
            "late_text": jsonlog.truncate("".join(run.late), 4000) if run.late else None})
        self.emit(run.chat_id, "interrupted", {"turn_id": run.turn_id, "by_turn_id": steer["turn_id"],
                                               "terminal_reason": "steered", "drain_ms": drain_ms, "forced": False,
                                               "unknown": []}, run.trace_id, task_id=run.task_id, turn_id=run.turn_id)
        self.log("turn end", run, event="turn_end", outcome="interrupted", detail="steered", latency_ms=drain_ms)
        with contextlib.suppress(ValueError):
            actor.queue.remove(steer["turn_id"])
        new = self._new_run(actor, steer)
        new.mode, new.rounds, new.timers = run.mode, run.rounds, run.timers
        run.timers = []
        if run.reply and run.reply.open:
            self.end_reply(run.chat_id, run.turn_id, run.reply, "gateway_closed", run.trace_id, run.task_id)
        actor.replies.pop(run.turn_id, None)
        actor.current = new
        task = self.store.task(new.task_id)
        self._start_turn(new, (task or {}).get("title"))
        return new

    def switch_to_task(self, run: TurnRun) -> Dict[str, Any]:
        """quick → task inside the running loop: Fable 5.1 from the next request, effort high from the next turn."""
        run.mode = models.TASK
        if run.task_id:
            self.store.update("tasks", "task_id", run.task_id, {"mode": models.TASK})
        self.store.update("turns", "turn_id", run.turn_id, {"mode": models.TASK})
        self.store.update("chats", "chat_id", run.chat_id, {"mode": models.TASK, "effort": models.EFFORT[models.TASK]})
        self.log("mode switch", run, event="mode_switch", outcome=models.TASK,
                 detail="start_task" if run.started_task else "quick round limit")
        return models.effort_message(models.EFFORT[models.TASK])

    def continue_after_limit(self, run: TurnRun, transcript: Transcript, stop: str) -> None:
        blocks = [self._unanswered_result(use) for use in transcript.open_tool_uses()]
        blocks.append({"type": "text", "text": "[continue]"})
        transcript.append([{"role": "user", "content": blocks}], run.turn_id)
        self.log("continue after limit", run, event="continue", detail=stop)

    def settled(self, run: TurnRun, message: Any) -> None:
        """A settling response's own client tool uses never run."""
        ids = [b.id for b in message.content if b.type == "tool_use"]
        for tool_use_id in ids:
            self.store.update("tool_calls", "tool_use_id", tool_use_id, {"status": "not_run", "ended_at": timeutil.iso()})
        self.log("server tool settled", run, event="settle", outcome=message.stop_reason,
                 detail=f"client_uses={len(ids)}")

    def on_compaction(self, chat_id: str, run: TurnRun) -> None:
        epoch = self.store.chat(chat_id)["epoch"]
        self.log("compaction", run, event="compaction", epoch=epoch)

    # ------------------------------------------------------------------ stream events of a turn

    def on_text(self, run: TurnRun, text: str, ttft: int) -> None:
        if run.first_text_at is None:
            run.first_text_at = time.monotonic()
            self.store.update("turns", "turn_id", run.turn_id, {"ttft_ms": ttft})
            self.log("first token", run, event="ttft", latency_ms=ttft)
            self.progress(run, "writing")
        reply = run.reply
        if reply is not None and reply.open and run.stop_reason is None:
            reply.spoken_any = True
            run.heard += text
            reply.coalescer.add(text)
        else:
            run.late.append(text)

    def on_tool_start(self, run: TurnRun, name: str, server: bool = False) -> None:
        reply = run.reply
        if reply is not None and reply.open and reply.spoken_any and not reply.hold_started:
            reply.hold_started = True
            reply.timers.append(asyncio.get_running_loop().call_later(
                REPLY_HOLD_S, lambda: self.end_reply(run.chat_id, run.turn_id, reply, "hold_elapsed", run.trace_id,
                                                     run.task_id)))
        if reply is not None and reply.coalescer:
            reply.coalescer.flush()
        kind = activity.kind_of(name, server, self.ha.is_read_only)
        if name in app_tools.NAMES:
            self.set_activity(run, kind)
            return
        run.step += 1
        self.set_activity(run, kind, emit=False)
        self.progress(run, "tool", tool=name, label=name)

    def on_progress_note(self, run: TurnRun, text: str) -> None:
        run.note_buffer += text

    def on_block_stop(self, run: TurnRun) -> None:
        if run.note_buffer.strip():
            run.last_note = run.note_buffer.strip()
            self.progress(run, "thinking", label=run.last_note)
        run.note_buffer = ""

    def transformations(self, run: TurnRun, entries: Any) -> None:
        for entry in entries:
            data = entry.to_dict() if hasattr(entry, "to_dict") else dict(entry)
            reason = data.get("reason")
            self.log("input transformation", run, event="input_transformations", code=str(reason),
                     detail=jsonlog.truncate(dumps(data), 200))
            if reason == "prefix_binding_mismatch":
                self.store.record_error("model", "transcript", jsonlog.truncate(dumps(data), 300), task_id=run.task_id,
                                        chat_id=run.chat_id, turn_id=run.turn_id, trace_id=run.trace_id)

    def account(self, run: TurnRun, message: Any, model: str, ttft: Optional[int], stream: Any) -> None:
        numbers = models.usage_numbers(message.usage)
        served = getattr(message, "model", None) or model
        cost = models.cost_usd(served, numbers)
        chat = self.store.chat(run.chat_id)
        transformations = [t.to_dict() if hasattr(t, "to_dict") else t
                           for t in (getattr(message, "input_transformations", None) or [])]
        request_id = getattr(stream, "request_id", None)
        self.store.record_usage({"ts": timeutil.iso(), "task_id": run.task_id, "chat_id": run.chat_id,
                                 "turn_id": run.turn_id, "epoch": chat["epoch"], "turn_kind": run.kind,
                                 "mode": run.mode, "effort": chat["effort"], "model": served,
                                 "request_id": request_id, "ttft_ms": ttft, "stop_reason": message.stop_reason,
                                 "cost_usd": cost, "input_transformations_json": dumps(transformations), **numbers})
        run.cost_usd += cost
        self.store.execute("UPDATE turns SET cost_usd = cost_usd + ?, model = ? WHERE turn_id = ?",
                           (cost, served, run.turn_id))
        if run.task_id:
            self.store.execute("UPDATE tasks SET cost_usd = cost_usd + ? WHERE task_id = ?", (cost, run.task_id))
        self.last_model_ok = time.time()
        if any(getattr(b, "type", None) == "fallback" for b in message.content):
            self.log("model fallback", run, event="model_fallback", model=served)
            self.store.record_error("model", "model_fallback", served, task_id=run.task_id, chat_id=run.chat_id,
                                    turn_id=run.turn_id, trace_id=run.trace_id)
        if transformations:
            self.transformations(run, transformations)
        self.log("model request", run, event="request", model=served, effort=chat["effort"], latency_ms=ttft,
                 cost_usd=round(cost, 5), outcome=message.stop_reason, detail=request_id,
                 code=f"in={models.total_input(numbers)} cache_read={numbers['cache_read_tokens']} "
                      f"out={numbers['output_tokens']}")

    def api_failure(self, run: TurnRun, error: Exception, model: str) -> Outcome:
        text = str(getattr(error, "message", "") or error)
        status = getattr(error, "status_code", None)
        self.last_api_error = f"{type(error).__name__} {status or ''}".strip()
        self.last_api_error_at = time.time()
        if isinstance(error, anthropic.BadRequestError) and "retention" in text.lower() and \
                model == models.MODELS[models.TASK]:
            self.store.update("chats", "chat_id", run.chat_id, {"task_model": models.RETENTION_MODEL})
            self.store.record_error("model", "retention", "task model switched to " + models.RETENTION_MODEL,
                                    task_id=run.task_id, chat_id=run.chat_id, turn_id=run.turn_id, trace_id=run.trace_id)
            self.log("retention fallback", run, level="warning", event="model_fallback", model=models.RETENTION_MODEL)
            return Outcome("retry", code="retention")
        if isinstance(error, anthropic.RateLimitError):
            code = "rate_limit"
        elif isinstance(error, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            code = "auth"
        elif isinstance(error, anthropic.BadRequestError) and ("credit" in text.lower() or "billing" in text.lower()):
            code = "billing"
        elif isinstance(error, (anthropic.APIStatusError, anthropic.APIConnectionError)):
            code = "api_error"
        else:
            code = "sdk_error"
        self.store.record_error("model", code, jsonlog.truncate(jsonlog.redact_text(text), 300), task_id=run.task_id,
                                chat_id=run.chat_id, turn_id=run.turn_id, trace_id=run.trace_id)
        self.log("api error", run, level="warning", event="api_error", code=code,
                 detail=jsonlog.truncate(f"{type(error).__name__} {status or ''}", 80))
        return Outcome("failed", code=code, message=f"{type(error).__name__}" + (f" {status}" if status else ""))

    # ------------------------------------------------------------------ tool calls of a turn

    def _tool_kind(self, name: str) -> str:
        if name == "device_action":
            return "device"
        if name in app_tools.NAMES:
            return "app"
        if name == "memory":
            return "memory"
        return "ha"

    def record_tool_uses(self, run: TurnRun, message: Any) -> None:
        chat = self.store.chat(run.chat_id)
        results = {getattr(b, "tool_use_id", None): b for b in message.content
                   if str(getattr(b, "type", "")).endswith("_tool_result")}
        for tool_use_id, result in results.items():
            row = self.store.one("SELECT status FROM tool_calls WHERE tool_use_id = ?", (tool_use_id,))
            if row and row["status"] == "pending":
                self.store.update("tool_calls", "tool_use_id", tool_use_id, {
                    "status": self._server_status(result), "ended_at": timeutil.iso()})
        for block in message.content:
            if block.type not in ("tool_use", "server_tool_use"):
                continue
            server = block.type == "server_tool_use"
            kind = "server" if server else self._tool_kind(block.name)
            args = dict(block.input or {}) if isinstance(block.input, dict) else {}
            if kind == "ha":
                read_only = self.ha.is_read_only(block.name)
                target = describe_call(block.name, args)[1]
            elif kind == "device":
                read_only = str(args.get("tool")) not in app_tools.DEVICE_SETTERS
                target = jsonlog.truncate(str(args.get("tool") or ""), 128)
            else:
                read_only, target = True, None
            status = "running"
            if server:
                result = results.get(block.id)
                status = "pending" if result is None and message.stop_reason in ("tool_use", "pause_turn") else \
                    self._server_status(result)
            self.store.insert("tool_calls", {
                "tool_use_id": block.id, "task_id": run.task_id, "chat_id": run.chat_id, "turn_id": run.turn_id,
                "epoch": chat["epoch"], "kind": kind, "tool": block.name,
                "args_summary": jsonlog.redact_args(args), "target": target, "read_only": int(read_only),
                "status": status, "started_at": timeutil.iso(),
                "ended_at": timeutil.iso() if server and status != "pending" else None}, or_ignore=True)
            self.log("tool call", run, tool=block.name, event="tool_use", outcome=kind,
                     detail=jsonlog.truncate(dumps(jsonlog.redact_args(args)), 200))

    @staticmethod
    def _server_status(result: Any) -> str:
        content = getattr(result, "content", None)
        return "error" if result is None or str(getattr(content, "type", "")).endswith("error") else "ok"

    def record_tool_results(self, run: TurnRun, response: Optional[Dict[str, Any]]) -> None:
        """Each client tool's status; an HA call's is the adapter's disposition for its tool use."""
        for block in (response or {}).get("content") or []:
            if block.get("type") != "tool_result":
                continue
            disposition = self.ha.take_disposition(block["tool_use_id"])
            row = self.store.one("SELECT * FROM tool_calls WHERE tool_use_id = ?", (block["tool_use_id"],))
            if row is None or row["status"] != "running":
                continue
            content = block.get("content")
            text = content if isinstance(content, str) else "\n".join(
                c.get("text", "") for c in content or [] if isinstance(c, dict))
            values: Dict[str, Any] = {}
            if row["kind"] == "ha" and disposition is not None:
                approval_id, kind = disposition
                status = DISPOSITION_STATUS.get(kind, "unknown")
                if approval_id and kind != "blocked":
                    values["approval_id"] = approval_id
            elif row["kind"] == "ha":
                status = ("pending" if text.startswith("PENDING_APPROVAL") else
                          "unknown" if text in (UNKNOWN_TEXT, BLOCKED_TEXT) else
                          "not_run" if text in (UNREACHABLE_TEXT, GUARD_TEXT) else
                          "error" if block.get("is_error") else "ok")
            else:
                status = "error" if block.get("is_error") else "ok"
            started = timeutil.parse(row["started_at"])
            values.update(status=status, result_summary=jsonlog.truncate(jsonlog.redact_text(text or ""), 200),
                          ended_at=None if status == "pending" else timeutil.iso(),
                          duration_ms=None if status == "pending" else
                          int((timeutil.now() - started).total_seconds() * 1000))
            self.store.update("tool_calls", "tool_use_id", block["tool_use_id"], values)
            self.log("tool result", run, tool=row["tool"], event="tool_result", outcome=status)

    def set_activity(self, run: TurnRun, kind: Optional[str], emit: bool = True) -> None:
        """The task's activity label for kind (None clears it, only from the turn that set it last); a change goes
        out at most once per ACTIVITY_MIN_INTERVAL_S per task, the latest one when the interval ends."""
        if run.task_id is None:
            return
        state = self.activities.get(run.task_id)
        if state is None:
            if kind is None:
                return
            state = self.activities[run.task_id] = Activity(run=run)
        elif kind is None and state.run is not run:
            return
        state.run = run
        state.want = activity.label(kind, run.lang) if kind else None
        if state.timer is not None and kind is not None:
            return
        if state.timer is not None:
            state.timer.cancel()
            state.timer = None
        wait = state.at + ACTIVITY_MIN_INTERVAL_S - time.monotonic()
        if wait > 0 and state.want != state.label and not self._task_ended(run.task_id):
            state.timer = asyncio.get_running_loop().call_later(wait, self._activity_due, run.task_id)
            return
        self._apply_activity(run.task_id, state, emit)

    def _task_ended(self, task_id: str) -> bool:
        task = self.store.task(task_id)
        return task is None or task["state"] in TERMINAL_STATES

    def _activity_due(self, task_id: str) -> None:
        state = self.activities.get(task_id)
        if state is not None:
            state.timer = None
            self._apply_activity(task_id, state, True)

    def _apply_activity(self, task_id: str, state: Activity, emit: bool) -> None:
        task = self.store.task(task_id)
        if task is None or task["state"] in TERMINAL_STATES:
            if state.timer is not None:
                state.timer.cancel()
            self.activities.pop(task_id, None)
            return
        if task["state"] != "progress" or state.want == state.label:
            state.want = state.label
            return
        state.label, state.at = state.want, time.monotonic()
        self.log("activity", state.run, event="activity", detail=state.label or "cleared")
        if emit:
            self.progress(state.run, state.run.last_phase or "thinking")

    def progress(self, run: TurnRun, phase: str, tool: Optional[str] = None, label: Optional[str] = None,
                 force: bool = False) -> None:
        if run.task_id is None:
            return
        now = time.monotonic()
        shown = self.activities.get(run.task_id)
        current = shown.label if shown else None
        if not force and phase == run.last_phase and current == run.last_activity and \
                now - run.last_progress < PROGRESS_MIN_INTERVAL_S:
            return
        run.last_progress, run.last_phase, run.last_activity = now, phase, current
        progress: Dict[str, Any] = {"phase": phase, "step": run.step or None, "tool": tool}
        if label:
            progress["label"] = jsonlog.truncate(label, 40)
        if current:
            progress["activity"] = current
        self.store.update("tasks", "task_id", run.task_id, {"progress_json": progress})
        self.emit(run.chat_id, "progress", progress, run.trace_id, task_id=run.task_id, turn_id=run.turn_id)
        if not self.reply_open(run):
            task = self.store.task(run.task_id)
            # A task waiting on its card keeps the card on the device.
            if task and task["state"] not in TERMINAL_STATES + ("needs_input", "needs_approval"):
                self.outbox.enqueue(run.device_id, "agent", "SILENT", f"task:{run.task_id}",
                                    agent_payload(task, "progress", progress=progress), task_id=run.task_id,
                                    chat_id=run.chat_id, turn_id=run.turn_id, lang=run.lang)

    # ------------------------------------------------------------------ app tools

    async def app_tool(self, actor: ChatActor, name: str, args: Dict[str, Any]) -> str:
        run = actor.current
        chat_id = actor.chat_id
        if name == "ask_user":
            if run is None or run.task_id is None:
                return "No task is active."
            return await self.interactions.ask(run, args)
        if name == "start_task":
            if run is None:
                return "No turn is active."
            run.started_task = True
            if self.reply_open(run) and not run.reply.spoken_any:
                self.emit(run.chat_id, "text", {"delta": routing.ACK[run.lang]}, run.trace_id, task_id=run.task_id,
                          turn_id=run.turn_id)
                run.reply.spoken_any = True
            title = jsonlog.truncate(str(args.get("title") or ""), TITLE_MAX)
            if title and run.task_id:
                self.store.update("tasks", "task_id", run.task_id, {"title": title})
            return STARTED_TEXT
        if name == "set_chat_title":
            if args.get("title"):
                self.chats.set_title(chat_id, str(args["title"]))
            return "Title set."
        if name == "device_action":
            return await self._device_action(run, args, actor.take_use(name, args))
        if name == "set_result_tier":
            tier = {"interrupt": "INTERRUPT", "silent": "SILENT"}.get(str(args.get("tier")), "WHEN_IDLE")
            if run is not None and run.task_id:
                self.store.update("tasks", "task_id", run.task_id, {"result_tier": tier})
            return f"Result tier set to {tier}."
        if name == "notify_user":
            return self._notify(run, chat_id, args)
        return "Unknown tool."

    def _notify(self, run: Optional[TurnRun], chat_id: str, args: Dict[str, Any]) -> str:
        """A notice: an unread `agent` card of its own (`ntf_…`), spoken at its tier (protocol: Notices)."""
        text = jsonlog.truncate(jsonlog.redact_text(str(args.get("text") or "")), 300)
        if not text:
            return "Nothing to say."
        chat = self.store.chat(chat_id)
        urgent = bool(args.get("urgent"))
        lang = run.lang if run else "en"
        notice_id = new_id("ntf")
        payload = {"task_id": notice_id, "chat_id": chat_id, "state": "unread", "title": NOTICE_TITLE[lang],
                   "summary": jsonlog.truncate(text, SUMMARY_MAX), "lang": lang, "notice": True,
                   "origin_task_id": run.task_id if run else None}
        self.outbox.enqueue(chat["device_id"], "agent", "INTERRUPT" if urgent else "WHEN_IDLE",
                            f"notify:{notice_id}", payload, task_id=notice_id, chat_id=chat_id,
                            turn_id=run.turn_id if run else None, speak=text, lang=lang, ttl_s=NOTIFY_TTL_S)
        if urgent:
            return "Queued: the device speaks it now, interrupting what it is doing, and shows it as a card."
        return "Queued: the device speaks it when it is idle and shows it as a card."

    def _mark_device_call(self, tool_use_id: Optional[str], values: Dict[str, Any]) -> None:
        if tool_use_id:
            self.store.update("tool_calls", "tool_use_id", tool_use_id, values)

    async def _device_action(self, run: Optional[TurnRun], args: Dict[str, Any],
                             tool_use_id: Optional[str] = None) -> str:
        """A device tool call: a `device_action` event on the turn's open reply stream; the gateway runs it on the
        device and posts the result (device_action_result). Dispatch and disposition are persisted on the
        tool_calls row; the action id is derived from the tool use, so a repeat of the same use keeps it."""
        tool = str(args.get("tool") or "")
        params = args.get("args") or {}
        refused = None
        if tool not in app_tools.DEVICE_TOOLS:
            refused = "Unknown device tool. Available: " + ", ".join(app_tools.DEVICE_TOOLS) + "."
        elif not isinstance(params, dict):
            refused = "args must be an object."
        elif run is None or not self.reply_open(run):
            refused = ("The device is not in a conversation right now, so device actions cannot run. "
                       "Tell the user to ask again while talking to you.")
        if refused is not None:
            self._mark_device_call(tool_use_id, {"status": "not_run", "ended_at": timeutil.iso()})
            return refused
        action_id = ("dac_" + hashlib.sha256(tool_use_id.encode()).hexdigest()[:26]) if tool_use_id else new_id("dac")
        future = asyncio.get_running_loop().create_future()
        self.device_actions[action_id] = (future, run.chat_id)
        self._mark_device_call(tool_use_id, {"status": "dispatched", "dispatch_id": action_id})
        self.emit(run.chat_id, "device_action", {"action_id": action_id, "tool": tool, "args": params},
                  run.trace_id, task_id=run.task_id, turn_id=run.turn_id)
        self.log("device action", run, tool=tool, event="device_action", detail=action_id)
        try:
            result = await asyncio.wait_for(future, DEVICE_ACTION_WAIT_S)
        except asyncio.TimeoutError:
            self.log("device action timeout", run, level="warning", tool=tool, event="device_action",
                     outcome="timeout", detail=action_id)
            self._mark_device_call(tool_use_id, {"status": "unknown", "ended_at": timeutil.iso(),
                                                 "result_summary": "no answer from the device"})
            return "The device did not answer in time; the outcome is unknown. Do not repeat it."
        finally:
            self.device_actions.pop(action_id, None)
        self.log("device action result", run, tool=tool, event="device_action",
                 outcome="ok" if result["ok"] else "error", detail=action_id)
        text = ("Device result: " + (result.get("result") or "done")) if result["ok"] else \
            ("The device reported an error: " + (result.get("error") or "unknown error"))
        self._mark_device_call(tool_use_id, {"status": "ok" if result["ok"] else "error", "ended_at": timeutil.iso(),
                                             "result_summary": jsonlog.truncate(text, 200)})
        return jsonlog.redact_text(text)

    def device_action_result(self, chat_id: str, action_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        entry = self.device_actions.get(action_id)
        if entry is None or entry[1] != chat_id:
            raise ApiError(404, "not_found", "no device action is waiting for this result")
        future = entry[0]
        if future.done():
            raise ApiError(409, "invalid_state", "this device action already has a result")
        future.set_result({"ok": bool(body["ok"]),
                           "result": jsonlog.truncate(body.get("result") or "", DEVICE_RESULT_MAX) or None,
                           "error": body.get("error")})
        return {"action_id": action_id, "chat_id": chat_id, "accepted": True}

    # ------------------------------------------------------------------ stopping a turn (steer, barge-in, cancel, timeout)

    def _on_steer(self, actor: ChatActor, turn_id: str) -> None:
        """Nothing of the running turn is spoken from now on; a streaming reply is cancelled after STEER_WAIT_S."""
        run = actor.current
        if run is None:
            return
        run.stop_at = run.stop_at or time.monotonic()
        if run.reply and run.reply.open:
            self.end_reply(run.chat_id, run.turn_id, run.reply, "gateway_closed", run.trace_id, run.task_id,
                           flush=False)
        self.interactions.release_for_steer(run)

        def check(request_marker: Any) -> None:
            current = actor.current
            if current is None or actor.stream is None or actor.stream is not request_marker:
                return
            if actor.streaming and not actor.resuming_pause and current.stop_reason is None:
                self.log("steer wait elapsed", current, event="steer", outcome="cancel")
                self._stop(actor, "steer", turn_id)

        if actor.streaming and not actor.resuming_pause:
            marker = actor.stream
            asyncio.get_running_loop().call_later(STEER_WAIT_S, check, marker)

    def _stop(self, actor: ChatActor, reason: str, by_turn_id: Optional[str]) -> None:
        run = actor.current
        if run is None or actor.loop is None or actor.loop.done() or actor.finishing:
            return
        if run.stop_reason is None:
            run.stop_reason, run.stop_by = reason, by_turn_id
            run.stop_at = run.stop_at or time.monotonic()
        self.interactions.deny_pending_gates(run, "The user stopped this." if reason == "cancel"
                                             else "No answer; the user's new message follows.")
        actor.loop.cancel()

    def _turn_timeout(self, actor: ChatActor) -> None:
        run = actor.current
        if run is not None and run.stop_reason is None:
            self.log("turn runaway guard", run, level="warning", event="timeout")
            self._stop(actor, "timeout", None)

    async def _stopped(self, actor: ChatActor, run: TurnRun) -> None:
        """The loop of `run` was cancelled: the partial reply is gone, the uncommitted tail stays."""
        drain_ms = int((time.monotonic() - run.stop_at) * 1000) if run.stop_at else None
        reason = run.stop_reason or "cancelled"
        terminal = {"steer": "steered", "barge_in": "barge_in", "cancel": "cancelled", "timeout": "timeout"}.get(
            reason, reason)
        unknown = self._unknown_effects(run)
        self.store.update("turns", "turn_id", run.turn_id, {
            "state": "interrupted" if reason != "timeout" else "failed", "terminal_reason": terminal,
            "ended_at": timeutil.iso(), "interrupted_by": run.stop_by,
            "late_text": jsonlog.truncate("".join(run.late), 4000) if run.late else None})
        self.emit(run.chat_id, "interrupted", {"turn_id": run.turn_id, "by_turn_id": run.stop_by,
                                               "terminal_reason": terminal, "drain_ms": drain_ms, "forced": True,
                                               "unknown": unknown}, run.trace_id, task_id=run.task_id,
                  turn_id=run.turn_id)
        self.log("turn end", run, event="turn_end", outcome="interrupted", detail=terminal, latency_ms=drain_ms)
        if run.heard and reason in ("steer", "barge_in"):
            self.chats.add_note(run.chat_id, "note", f'[note: your previous reply was cut off; the user heard: '
                                                     f'"{jsonlog.truncate(run.heard, 300)}"]')
        ha_running = self.store.all("SELECT tool, args_json FROM approvals WHERE turn_id = ? AND state = 'in_flight'",
                                    (run.turn_id,))
        calls = [describe_call(r["tool"], loads(r["args_json"], {}))[0] for r in ha_running]
        if calls:
            self.chats.add_note(run.chat_id, "note", f"[note: Home Assistant calls still running: {'; '.join(calls)}; "
                                                     "their outcome will arrive as an event; do not repeat them]")
        if reason == "timeout":
            await self._turn_failed(run, "timeout", "turn exceeded its time limit", recorded=True)
        elif reason == "barge_in":
            asyncio.get_running_loop().call_later(BARGE_IN_RESUME_S, self._after_barge_in, actor.chat_id, run.task_id,
                                                  run.turn_id)

    def _after_barge_in(self, chat_id: str, task_id: Optional[str], turn_id: str) -> None:
        """No message followed a barge-in: task work resumes; a quick reply ends there."""
        actor = self.rt(chat_id)
        task = self.store.task(task_id) if task_id else None
        if actor.busy or task is None or task["state"] in TERMINAL_STATES:
            return
        newer = self.store.scalar("SELECT COUNT(*) FROM turns WHERE chat_id = ? AND created_at > "
                                  "(SELECT created_at FROM turns WHERE turn_id = ?)", (chat_id, turn_id))
        if newer:
            return
        if task["mode"] == models.TASK:
            chat = self.store.chat(chat_id)
            self._enqueue(chat, task, "continue", "", task["lang"], task["trace_id"], None, None, kind="event")
        else:
            self.spawn(self.finish_task(task_id, "cancelled", reason="voice"), f"barge-end:{task_id}")

    async def cancel(self, chat_id: str, reason: str) -> Optional[Dict[str, Any]]:
        """Interrupt route: stop the turn in flight and cancel the open task."""
        actor = self.rt(chat_id)
        task = self.open_task(chat_id)
        if actor.current is not None and actor.loop is not None:
            loop = actor.loop
            self._stop(actor, "cancel", None)
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(loop), timeout=CANCEL_MAX_S)
            await self._wait_idle(actor, CANCEL_MAX_S)
        for turn_id in list(actor.queue):
            self.store.update("turns", "turn_id", turn_id, {"state": "failed", "terminal_reason": "cancelled",
                                                           "ended_at": timeutil.iso()})
            reply = actor.replies.pop(turn_id, None)
            if reply is not None:
                turn = self.store.turn(turn_id)
                self.end_reply(chat_id, turn_id, reply, "turn_done", turn["trace_id"], turn["task_id"])
        actor.queue.clear()
        task = self.store.task(task["task_id"]) if task else self.open_task(chat_id)
        if task is None or task["state"] in TERMINAL_STATES:
            return task
        await self.interactions.withdraw_task(task["task_id"])
        self.chats.add_note(chat_id, "note", "[note: the user stopped your previous work]")
        await self.finish_task(task["task_id"], "cancelled", reason=reason)
        return self.store.task(task["task_id"])

    async def _wait_idle(self, actor: ChatActor, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while actor.current is not None and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

    async def _voice_stop(self, chat: Dict[str, Any], task: Optional[Dict[str, Any]], lang: str, trace: str,
                          reply_id: str) -> Dict[str, Any]:
        accepted = self.emit(chat["chat_id"], "accepted", {"routed": "cancel", "task_state": task["state"] if task else None,
                                                           "reply_id": reply_id},
                             trace, task_id=task["task_id"] if task else None)
        cancelled = await self.cancel(chat["chat_id"], "voice")
        effects = loads(cancelled["side_effects_json"], []) if cancelled else []
        text = routing.STOPPED[lang]
        if any(e["status"] == "unknown" for e in effects):
            text += routing.MAYBE_HAPPENED[lang]
        task_id = cancelled["task_id"] if cancelled else None
        self.emit(chat["chat_id"], "text", {"delta": text}, trace, task_id=task_id)
        self.emit(chat["chat_id"], "reply_end", {"reason": "ack", "ack_text": text, "reply_id": reply_id}, trace,
                  task_id=task_id)
        return {"chat_id": chat["chat_id"], "task_id": task_id, "turn_id": None, "routed": "cancel",
                "reply_id": reply_id, "after": accepted["seq"] - 1, "duplicate": False, "trace_id": trace,
                "task_state": cancelled["state"] if cancelled else None}

    # ------------------------------------------------------------------ end of a turn

    def _unknown_effects(self, run: TurnRun) -> List[Dict[str, Any]]:
        """Side-effect tool uses of this turn that were sent without a result: their outcome is unknown."""
        rows = self.store.all("SELECT * FROM tool_calls WHERE turn_id = ? AND read_only = 0 AND "
                              "((kind = 'ha' AND status = 'running') OR (kind = 'device' AND status = 'dispatched'))",
                              (run.turn_id,))
        return [{"tool": row["tool"], "target": row["target"], "status": "unknown"} for row in rows]

    def side_effects(self, task_id: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Side effects of a task's non-read-only HA and device calls, and an outcome hint (denied/expired)."""
        effects = []
        raw = []
        for row in self.store.all("SELECT * FROM tool_calls WHERE task_id = ? AND read_only = 0 AND kind IN "
                                  "('ha', 'device') ORDER BY started_at", (task_id,)):
            raw.append(row["status"])
            status = EFFECT_STATUS.get(row["status"], "unknown")
            effects.append({"tool": row["tool"], "target": row["target"], "status": status})
        hint = None
        if raw and "ok" not in raw and all(s in ("denied", "expired", "error", "not_run") for s in raw):
            hint = "denied" if "denied" in raw else "expired" if "expired" in raw else None
        return effects, hint

    @staticmethod
    def outcome_of(state: str, effects: List[Dict[str, Any]], hint: Optional[str] = None) -> str:
        statuses = {e["status"] for e in effects}
        if "unknown" in statuses:
            return "unknown"
        if hint:
            return hint
        if state == "done":
            if effects and statuses == {"not_run"}:
                return "none"
            return "partial" if "failed" in statuses and "confirmed" in statuses else "ok"
        if "confirmed" in statuses:
            return "partial"
        return "none"

    async def finish_drive(self, actor: ChatActor, run: TurnRun, outcome: Optional[Outcome]) -> None:
        if run.reply is not None and run.reply.coalescer:
            run.reply.coalescer.flush()
        if outcome is None:
            outcome = Outcome("failed", code="internal", message="the loop ended without an outcome")
        if outcome.kind == "retry":
            actor.retry = True
            return
        now = timeutil.iso()
        values = {"ended_at": now, "late_text": jsonlog.truncate("".join(run.late), 4000) if run.late else None,
                  "final_text": jsonlog.truncate(outcome.text, 2000) if outcome.text else None}
        if outcome.kind == "end_turn":
            self.store.update("turns", "turn_id", run.turn_id, dict(values, state="completed", terminal_reason="end_turn"))
            self.log("turn end", run, event="turn_end", outcome="completed",
                     latency_ms=int((time.monotonic() - (run.sent_at or time.monotonic())) * 1000), cost_usd=round(run.cost_usd, 5))
            await self._turn_completed(actor, run, outcome.text.strip())
            return
        self.store.update("turns", "turn_id", run.turn_id, dict(values, state="failed", terminal_reason=outcome.code))
        await self._turn_failed(run, outcome.code or "internal", outcome.message or "failed", recorded=True)

    async def _turn_completed(self, actor: ChatActor, run: TurnRun, final_text: str) -> None:
        task = self.store.task(run.task_id) if run.task_id else None
        if task is None or task["state"] in TERMINAL_STATES:
            if run.reply:
                self.end_reply(run.chat_id, run.turn_id, run.reply, "turn_done", run.trace_id, run.task_id)
            return
        if not final_text and run.last_note and self.reply_open(run) and not run.reply.spoken_any:
            final_text = run.last_note
            self.emit(run.chat_id, "text", {"delta": final_text}, run.trace_id, task_id=run.task_id, turn_id=run.turn_id)
            run.reply.spoken_any = True
        spoken_inline = self.reply_open(run)
        if run.stop_reason == "steer" or any((self.store.turn(t) or {}).get("task_id") == task["task_id"]
                                             for t in actor.queue):
            self.chats.add_note(run.chat_id, "note",
                                f'[note: your work on "{task["title"]}" finished just before this message; result: '
                                f'"{jsonlog.truncate(final_text, 400)}"; the user has not heard it]')
            if run.reply:
                self.end_reply(run.chat_id, run.turn_id, run.reply, "turn_done", run.trace_id, run.task_id)
            self.outbox.enqueue(run.device_id, "agent", "SILENT", f"task:{task['task_id']}",
                                agent_payload(task, "progress", summary=jsonlog.truncate(final_text, SUMMARY_MAX)),
                                task_id=task["task_id"], chat_id=run.chat_id, turn_id=run.turn_id, lang=run.lang)
            return
        if self.interactions.open_for_task(task["task_id"]) or self.chats.pending_notes(run.chat_id, kind="event"):
            if run.reply and run.reply.open:
                self.end_reply(run.chat_id, run.turn_id, run.reply, "turn_done", run.trace_id, run.task_id)
            return
        if run.reply and run.reply.open:
            self.end_reply(run.chat_id, run.turn_id, run.reply, "turn_done", run.trace_id, run.task_id)
        speak = None if spoken_inline else (final_text or None)
        await self.finish_task(task["task_id"], "done", summary_text=final_text, speak=speak, turn_id=run.turn_id,
                               spoken_inline=spoken_inline)

    async def _turn_failed(self, run: TurnRun, code: str, message: str, recorded: bool = False) -> None:
        if not recorded:
            self.store.update("turns", "turn_id", run.turn_id, {"state": "failed", "terminal_reason": code,
                                                               "ended_at": timeutil.iso()})
        self.store.record_error("turn", code, message, task_id=run.task_id, chat_id=run.chat_id, turn_id=run.turn_id,
                                trace_id=run.trace_id)
        self.log("turn end", run, level="warning", event="turn_end", outcome="failed", code=code)
        speak = FAIL_SPEAK.get(code, FAIL_SPEAK["default"])[run.lang]
        inline = self.speak_inline(run, speak, "turn_done")
        if run.reply is not None and run.reply.open:
            self.end_reply(run.chat_id, run.turn_id, run.reply, "turn_done", run.trace_id, run.task_id)
        if run.task_id:
            await self.finish_task(run.task_id, "failed", error={"code": code, "message": jsonlog.truncate(message, 300)},
                                   speak=None if inline else speak, turn_id=run.turn_id, spoken_inline=inline)

    async def finish_task(self, task_id: str, state: str, error: Optional[Dict[str, str]] = None,
                          speak: Optional[str] = None, reason: Optional[str] = None, outcome: Optional[str] = None,
                          summary_text: Optional[str] = None, turn_id: Optional[str] = None,
                          spoken_inline: bool = False, unread: bool = True) -> None:
        task = self.store.task(task_id)
        if task is None or task["state"] in TERMINAL_STATES:
            return
        effects, hint = self.side_effects(task_id)
        result_outcome = outcome or self.outcome_of(state, effects, hint)
        summary = jsonlog.truncate(summary_text, SUMMARY_MAX) if summary_text else task["summary"]
        self.interactions.close_task_interactions(task)
        retry_allowed = not any(e["status"] == "unknown" for e in effects)
        now = timeutil.iso()
        self.set_task_state(task_id, state, reason or state, turn_id=turn_id, outcome=result_outcome,
                            error_json=error or None, side_effects_json=effects,
                            summary=summary, finished_at=now, retry_allowed=int(retry_allowed))
        chat = self.store.chat(task["chat_id"])
        if chat and chat["open_task_id"] == task_id:
            self.store.update("chats", "chat_id", task["chat_id"], {"open_task_id": None, "mode": models.QUICK})
        task = self.store.task(task_id)
        tier = task["result_tier"]
        if state == "done":
            data = {"summary": summary, "speak": speak, "tier": tier, "outcome": result_outcome, "side_effects": effects}
        elif state == "failed":
            data = {"error": error, "speak": speak, "outcome": result_outcome, "side_effects": effects}
        else:
            data = {"reason": reason or "api", "speak": speak, "outcome": result_outcome, "side_effects": effects}
        self.emit(task["chat_id"], state, data, task["trace_id"], task_id=task_id, turn_id=turn_id)
        self.chats.set_summary(task["chat_id"], summary)
        self.chats.touch(task["chat_id"])
        options = None
        if state == "failed":
            options = [{"id": "retry", "label": "Retry"}] if retry_allowed else [{"id": "check", "label": "Check state"}]
        payload = agent_payload(task, state, unread=unread, side_effects=effects, options=options)
        if state == "cancelled" or spoken_inline or not speak:
            self.outbox.enqueue(task["device_id"], "agent", "SILENT", f"task:{task_id}", payload, task_id=task_id,
                                chat_id=task["chat_id"], turn_id=turn_id, lang=task["lang"])
        else:
            delivery_tier = tier if state == "done" else "WHEN_IDLE"
            self.outbox.enqueue(task["device_id"], "agent", delivery_tier, f"task:{task_id}", payload, task_id=task_id,
                                chat_id=task["chat_id"], turn_id=turn_id,
                                speak=None if delivery_tier == "SILENT" else speak, lang=task["lang"],
                                ttl_s=DONE_SPEECH_TTL_S)
        created = timeutil.parse(task["created_at"])
        jsonlog.log("task finished", task_id=task_id, chat_id=task["chat_id"], device_id=task["device_id"],
                    trace_id=task["trace_id"], event="task_state", outcome=f"{state}/{result_outcome}",
                    latency_ms=int((timeutil.now() - created).total_seconds() * 1000), cost_usd=task["cost_usd"],
                    code=(error or {}).get("code"))

    # ------------------------------------------------------------------ lifecycle

    async def prewarm(self, chat: Dict[str, Any]) -> None:
        """A switched-to chat: its actor and frozen prompt are loaded; nothing is interrupted."""
        self.rt(chat["chat_id"])
        self.prompt(chat["chat_id"])

    async def shutdown(self) -> None:
        self.closed = True
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(BaseException):
                await task
