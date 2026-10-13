"""Interactions: ask_user questions, approvals, decisions and the HA approval listener."""
import asyncio
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from . import jsonlog, routing, timeutil
from .config import (
    INPUT_TTL_S, OPTION_WIDTH_MAX, OPTIONS_MAX, OPTIONS_MIN, QUESTION_MAX, SUMMARY_MAX, VOICE_WINDOW_S,
)
from .deliveries import agent_payload
from .ha_adapter import AlreadyDecided, NotOwner, Uncorrelated, describe_call, reversible
from .ids import new_id
from .store import TERMINAL_STATES, dumps, loads

if TYPE_CHECKING:
    from .turns import Engine, TurnRun

LATE_ANSWER = "The user has not answered yet; end the turn. Their answer will arrive as a new message."
STEER_ANSWER = "No answer; the user's new message follows."
OPEN_STATES = ("pending", "uncorrelated")
UNCORRELATED_TEXT = {"en": "This may still be waiting in the Home Assistant panel.",
                     "zh": "这个可能还在 Home Assistant 面板里等待确认。"}
NOT_ASKED = ("Not asked: {why}. ask_user is only for a real choice: one short question and 2-4 distinct short "
             "options. Answer directly in your reply, ask there if you must, and end the turn.")
PANEL_CUE = {"en": "That needs approval in the Home Assistant panel.", "zh": "这个需要在 Home Assistant 面板里确认。"}


def panel_cue(lang: str, action_text: str) -> str:
    if lang == "zh":
        return f"要{action_text}，需要在 Home Assistant 面板里确认。"
    return f"To {action_text}, approve it in the Home Assistant panel."


def label_width(text: str) -> int:
    return sum(3 if ord(ch) >= 0x1100 else 1 for ch in text)


def check_question(args: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(question, None) for a valid ask_user call, else (None, why it was not asked)."""
    text = " ".join(str(args.get("question") or "").split())
    if not text:
        return None, "no question given"
    if len(text) > QUESTION_MAX:
        return None, f"the question is longer than {QUESTION_MAX} characters"
    if routing.is_open_ended(text):
        return None, "an open-ended question is not a choice"
    raw = args.get("options")
    if not isinstance(raw, list):
        return None, "no options given"
    labels = [" ".join(str(o).split()) if isinstance(o, (str, int, float)) else "" for o in raw]
    if not OPTIONS_MIN <= len(labels) <= OPTIONS_MAX:
        return None, f"{len(labels)} options given"
    if not all(labels):
        return None, "an option is empty"
    if any(label_width(label) > OPTION_WIDTH_MAX for label in labels):
        return None, f"an option is longer than {OPTION_WIDTH_MAX} letters (4 Chinese characters)"
    if len({label.casefold() for label in labels}) != len(labels):
        return None, "two options are the same"
    options = [{"id": f"opt{i + 1}", "label": label} for i, label in enumerate(labels)]
    return {"text": text, "options": options, "multi_select": bool(args.get("multi_select"))}, None


def has_choice(question: Optional[Dict[str, Any]]) -> bool:
    return OPTIONS_MIN <= len((question or {}).get("options") or []) <= OPTIONS_MAX


class Interactions:
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self.store = engine.store
        self.chats = engine.chats
        self.outbox = engine.outbox

    # ------------------------------------------------------------------ views

    def public(self, row: Dict[str, Any]) -> Dict[str, Any]:
        out = {
            "interaction_id": row["interaction_id"], "task_id": row["task_id"], "chat_id": row["chat_id"],
            "turn_id": row["turn_id"], "kind": row["kind"], "source": row["source"], "state": row["state"],
            "voice_can_approve": bool(row["voice_can_approve"]), "created_at": row["created_at"],
            "expires_at": row["expires_at"], "decision": loads(row["decision_json"]), "via": row["via"],
        }
        if row["kind"] == "input":
            out["question"] = loads(row["question_json"])
        else:
            out["action"] = loads(row["action_json"])
        return out

    def open_for_task(self, task_id: str) -> List[Dict[str, Any]]:
        return self.store.all("SELECT * FROM interactions WHERE task_id = ? AND state IN ('pending', 'uncorrelated') "
                              "ORDER BY created_at", (task_id,))

    def held_note(self, task_id: str) -> Optional[str]:
        """System note for a turn of a task whose approval is open: the held action stays open and is not re-issued."""
        rows = [r for r in self.open_for_task(task_id) if r["kind"] == "approval"]
        if not rows:
            return None
        held = "; ".join(f"{(loads(r['action_json'], {}) or {}).get('text') or 'an action'} "
                         f"({'in the Home Assistant panel' if r['state'] == 'uncorrelated' else 'on the device'})"
                         for r in rows)
        return (f"[note: a previous action is waiting for approval: {held}. It stays open until the user decides or it "
                "expires; do not call it again. Its outcome arrives as an event.]")

    def _new(self, run_or_task: Any, kind: str, source: str, ttl_s: Optional[float], question=None, action=None,
             external_ref: Optional[str] = None, tool_use_id: Optional[str] = None, voice_can_approve: bool = False,
             expires_at: Optional[str] = None, turn_id: Optional[str] = None, state: str = "pending") -> Dict[str, Any]:
        if kind == "input" and not has_choice(question):
            raise ValueError("a question interaction needs 2-4 options")
        task = self.store.task(run_or_task if isinstance(run_or_task, str) else run_or_task.task_id)
        now = timeutil.now()
        interaction_id = new_id("int")
        self.store.insert("interactions", {
            "interaction_id": interaction_id, "task_id": task["task_id"], "chat_id": task["chat_id"],
            "turn_id": turn_id, "device_id": task["device_id"], "kind": kind, "source": source, "state": state,
            "question_json": question or None, "action_json": action or None,
            "voice_can_approve": int(voice_can_approve), "tool_use_id": tool_use_id, "external_ref": external_ref,
            "created_at": timeutil.iso(now),
            "expires_at": expires_at or (timeutil.iso(now + timedelta(seconds=ttl_s)) if ttl_s else None),
        })
        return self.store.interaction(interaction_id)

    def _speak_text(self, row: Dict[str, Any], lang: str) -> str:
        if row["kind"] == "input":
            return (loads(row["question_json"], {}) or {}).get("text") or ""
        action = loads(row["action_json"], {}) or {}
        if row["state"] == "uncorrelated":
            return panel_cue(lang, routing.describe_for_speech(action.get("text") or "do that"))
        return routing.approval_cue(lang, routing.describe_for_speech(action.get("text") or "do that"))

    def announce(self, row: Dict[str, Any], run: Optional["TurnRun"], speak: Optional[str] = None) -> bool:
        """Task state, event, inline cue (reply open) and the card. The card carries the cue WHEN_IDLE unless
        it was spoken inline: a question only when that reply is the device's voice turn, an approval in any
        reply. Returns whether the device's voice reply spoke it."""
        task = self.store.task(row["task_id"])
        if task is None or task["state"] in TERMINAL_STATES:
            return False
        state = "needs_input" if row["kind"] == "input" else "needs_approval"
        self.engine.set_task_state(task["task_id"], state, f"interaction {row['interaction_id']}",
                                   turn_id=row["turn_id"])
        speak = speak or self._speak_text(row, task["lang"])
        self.engine.emit(task["chat_id"], state, {"interaction": self.public(row), "speak": speak}, task["trace_id"],
                         task_id=task["task_id"], turn_id=row["turn_id"])
        jsonlog.log("interaction open", task_id=task["task_id"], chat_id=task["chat_id"], event=state,
                    detail=row["interaction_id"], outcome=row["source"])
        on_device = run is not None and run.reply is not None and run.reply.voice
        inline = run is not None and self.engine.speak_inline(run, speak, "question")
        on_device = on_device and inline
        if on_device:
            self.store.update("interactions", "interaction_id", row["interaction_id"], {"spoken_at": timeutil.iso()})
            self.open_voice_window(task["device_id"], row["interaction_id"])
        spoken = on_device if row["kind"] == "input" else inline
        self.card(row, speak=None if spoken else speak,
                  note=UNCORRELATED_TEXT[task["lang"]] if row["state"] == "uncorrelated" else None)
        return on_device

    def card(self, row: Dict[str, Any], speak: Optional[str] = None, note: Optional[str] = None) -> None:
        row = self.store.interaction(row["interaction_id"])
        task = self.store.task(row["task_id"])
        if task is None or row["state"] not in OPEN_STATES:
            return
        public = self.public(row)
        state = "needs_input" if row["kind"] == "input" else "needs_approval"
        timeout_s = None
        if row["expires_at"]:
            timeout_s = max(0, int((timeutil.parse(row["expires_at"]) - timeutil.now()).total_seconds()))
        options = None
        if row["kind"] == "input":
            options = (public.get("question") or {}).get("options") or None
        elif row["state"] == "pending":
            options = [{"id": "approve", "label": "Approve"}, {"id": "deny", "label": "Deny"}]
        summary = (public.get("question") or {}).get("text") if row["kind"] == "input" else \
            (public.get("action") or {}).get("text")
        payload = agent_payload(task, state, card_id=row["interaction_id"], interaction=public,
                                summary=jsonlog.truncate(note or summary, SUMMARY_MAX), options=options,
                                timeout_s=timeout_s,
                                risk=("reversible" if row["voice_can_approve"] else "irreversible")
                                if row["kind"] == "approval" else None)
        self.outbox.enqueue(task["device_id"], "agent", "WHEN_IDLE" if speak else "SILENT",
                            f"int:{row['interaction_id']}", payload, task_id=task["task_id"], chat_id=task["chat_id"],
                            turn_id=row["turn_id"], speak=speak, lang=task["lang"], ttl_s=timeout_s or INPUT_TTL_S)

    def open_voice_window(self, device_id: str, interaction_id: str) -> None:
        until = timeutil.iso(timeutil.now() + timedelta(seconds=VOICE_WINDOW_S))
        self.store.update("devices", "device_id", device_id,
                          {"voice_interaction_id": interaction_id, "voice_window_until": until})

    def resolved(self, row: Dict[str, Any], state: str, via: Optional[str]) -> None:
        task = self.store.task(row["task_id"])
        self.outbox.withdraw(row["device_id"], f"int:{row['interaction_id']}")
        if task is not None:
            self.engine.card_closed(task["task_id"])
            self.engine.emit(task["chat_id"], "resolved", {"interaction_id": row["interaction_id"], "state": state,
                                                           "via": via}, task["trace_id"], task_id=task["task_id"])
        jsonlog.log("interaction decided", task_id=row["task_id"], chat_id=row["chat_id"], event="resolved",
                    outcome=state, detail=row["interaction_id"], code=via)

    # ------------------------------------------------------------------ ask_user (held while its turn waits)

    async def ask(self, run: "TurnRun", args: Dict[str, Any]) -> str:
        """The `ask_user` app tool: the question is spoken inline while the reply is open and the call waits up
        to VOICE_WINDOW_S for the answer; otherwise it returns at once and the answer becomes a new turn."""
        question, why = check_question(args)
        if question is None:
            jsonlog.log("ask_user rejected", task_id=run.task_id, chat_id=run.chat_id, turn_id=run.turn_id,
                        event="interaction", outcome="rejected", detail=why)
            return NOT_ASKED.format(why=why)
        row = self._new(run, "input", "sdk", INPUT_TTL_S, question=question, turn_id=run.turn_id)
        reply_open = self.engine.reply_open(run) and run.stop_reason is None
        on_device = self.announce(row, run if reply_open else None, question["text"])
        if not reply_open:
            return LATE_ANSWER
        future = asyncio.get_running_loop().create_future()
        run.futures[row["interaction_id"]] = future
        try:
            kind, value = await asyncio.wait_for(future, timeout=VOICE_WINDOW_S)
        except asyncio.TimeoutError:
            if on_device:
                self.card(row, speak=question["text"])
            return LATE_ANSWER
        finally:
            run.futures.pop(row["interaction_id"], None)
        if kind == "answer":
            self.engine.set_task_state(run.task_id, self.engine.card_state(run.task_id) or "progress", "answered",
                                       turn_id=run.turn_id)
            return f"The user answered: {value if isinstance(value, str) else ', '.join(value)}"
        return str(value)

    def release_for_steer(self, run: "TurnRun") -> None:
        """A new message arrived while `ask_user` waits: the held call returns and the message follows."""
        self.deny_pending_gates(run, STEER_ANSWER)

    def deny_pending_gates(self, run: "TurnRun", message: str) -> None:
        for interaction_id, future in list(run.futures.items()):
            if not future.done():
                future.set_result(("stop", message))
            row = self.store.interaction(interaction_id)
            if row and row["state"] in OPEN_STATES:
                self.store.update("interactions", "interaction_id", interaction_id,
                                  {"state": "cancelled", "decided_at": timeutil.iso()})
                self.resolved(row, "cancelled", None)

    # ------------------------------------------------------------------ routing of spoken answers and decisions

    async def route_spoken(self, device_id: str, chat: Dict[str, Any], text: str, lang: str, trace: str,
                           reply_id: str) -> Optional[Dict[str, Any]]:
        device = self.store.device(device_id)
        if not device["voice_interaction_id"] or not device["voice_window_until"]:
            return None
        if timeutil.parse(device["voice_window_until"]) < timeutil.now():
            return None
        row = self.store.interaction(device["voice_interaction_id"])
        if row is None or row["state"] not in OPEN_STATES:
            return None
        task = self.store.task(row["task_id"])
        if task is None or task["state"] in TERMINAL_STATES:
            return None
        if row["kind"] == "input":
            self.store.update("devices", "device_id", device_id, {"voice_window_until": None})
            if not routing.answers_question(text, self._labels(row)):
                jsonlog.log("not an answer", task_id=task["task_id"], chat_id=task["chat_id"], event="interaction",
                            outcome="new_turn", detail=row["interaction_id"])
                return None
            return await self._voice_answer(row, task, text, lang, trace, reply_id)
        decision = routing.voice_decision(text)
        if decision is None:
            return None
        on_screen = self.store.scalar(
            "SELECT COUNT(*) FROM interactions WHERE device_id = ? AND kind = 'approval' AND state IN ('pending', 'uncorrelated')",
            (device_id,))
        action = loads(row["action_json"], {}) or {}
        speech_action = routing.describe_for_speech(action.get("text") or "")
        accepted = self.engine.emit(task["chat_id"], "accepted", {"routed": "approval", "task_state": task["state"],
                                                                  "reply_id": reply_id}, trace, task_id=task["task_id"])
        reason = "ack"
        if row["state"] == "uncorrelated":
            speak = PANEL_CUE[lang]
        elif decision == "deny":
            await self._decide_approval(row, "deny", "voice")
            speak = routing.DENIED[lang]
        elif decision == "approve" and row["voice_can_approve"] and on_screen == 1 and row["state"] == "pending":
            await self._decide_approval(row, "approve", "voice")
            speak = routing.OKAY[lang]
        else:
            speak = routing.repeat_back(lang, speech_action) if row["voice_can_approve"] else \
                routing.approval_cue(lang, speech_action)
            reason = "question"
            self.open_voice_window(device_id, row["interaction_id"])
        self.engine.emit(task["chat_id"], "text", {"delta": speak}, trace, task_id=task["task_id"])
        self.engine.emit(task["chat_id"], "reply_end", {"reason": reason, "ack_text": speak if reason == "ack" else None,
                                                        "reply_id": reply_id}, trace, task_id=task["task_id"])
        return {"chat_id": task["chat_id"], "task_id": task["task_id"], "turn_id": None, "routed": "approval",
                "reply_id": reply_id, "after": accepted["seq"] - 1, "duplicate": False, "trace_id": trace,
                "task_state": self.store.task(task["task_id"])["state"]}

    async def answer_by_message(self, task: Dict[str, Any], text: str, lang: str, trace: str,
                                reply_id: str) -> Optional[Dict[str, Any]]:
        """A voice message to a task waiting on its question, outside the voice window: the answer in the
        user's own words when `routing.answers_question` takes it as one, else None."""
        rows = [r for r in self.open_for_task(task["task_id"]) if r["kind"] == "input"]
        if not rows or not routing.answers_question(text, self._labels(rows[-1])):
            return None
        self.store.update("devices", "device_id", task["device_id"], {"voice_window_until": None})
        return await self._voice_answer(rows[-1], task, text, lang, trace, reply_id)

    def drop_questions(self, task: Dict[str, Any]) -> None:
        """A message that is not the answer: the task's open questions are withdrawn and the next turn says so."""
        for row in self.open_for_task(task["task_id"]):
            if row["kind"] != "input":
                continue
            self.store.update("interactions", "interaction_id", row["interaction_id"],
                              {"state": "cancelled", "decided_at": timeutil.iso()})
            self.resolved(row, "cancelled", None)
            question = (loads(row["question_json"], {}) or {}).get("text", "")
            self.chats.add_note(task["chat_id"], "note", f'[note: the user did not answer your question "{question}"; '
                                                         "their message is a new request and the question card is "
                                                         "withdrawn]")

    @staticmethod
    def _labels(row: Dict[str, Any]) -> List[str]:
        return [o["label"] for o in (loads(row["question_json"], {}) or {}).get("options") or []]

    async def _voice_answer(self, row: Dict[str, Any], task: Dict[str, Any], text: str, lang: str, trace: str,
                            reply_id: str) -> Dict[str, Any]:
        from .turns import Reply
        run = self.engine.current_run(task["chat_id"])
        future = run.futures.get(row["interaction_id"]) if run else None
        if future is not None and not future.done():
            accepted = self.engine.emit(task["chat_id"], "accepted", {"routed": "answer", "task_state": task["state"],
                                                                      "reply_id": reply_id},
                                        trace, task_id=task["task_id"], turn_id=run.turn_id)
            self.engine.attach_reply(run, Reply(reply_id=reply_id, opened_at=time.monotonic(), steer=True, voice=True))
            self._mark_answered(row, text, "voice", None)
            future.set_result(("answer", text))
            return {"chat_id": task["chat_id"], "task_id": task["task_id"], "turn_id": run.turn_id, "routed": "answer",
                    "reply_id": reply_id, "after": accepted["seq"] - 1, "duplicate": False, "trace_id": trace,
                    "task_state": "progress"}
        self._mark_answered(row, text, "voice", None)
        question = (loads(row["question_json"], {}) or {}).get("text", "")
        return await self.engine.answer_turn(task, f'[answer to your earlier question "{question}"]: {text}', lang,
                                             trace, reply_id, voice=True)

    def _mark_answered(self, row: Dict[str, Any], value: Any, via: str, decision_id: Optional[str]) -> None:
        self.store.update("interactions", "interaction_id", row["interaction_id"], {
            "state": "answered", "decision_json": {"answer": value}, "via": via, "decision_id": decision_id,
            "decided_at": timeutil.iso()})
        self.resolved(row, "answered", via)

    # ------------------------------------------------------------------ decision API

    def check_decision(self, decision_id: str, target_id: str, kind: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        from .turns import ApiError, body_hash
        row = self.store.one("SELECT * FROM decisions WHERE decision_id = ?", (decision_id,))
        if row is None:
            return None
        if row["target_id"] != target_id or row["kind"] != kind or row["body_hash"] != body_hash(body):
            raise ApiError(409, "already_decided", "decision_id was used for a different decision")
        return dict(loads(row["response_json"]), duplicate=True)

    def store_decision(self, decision_id: str, target_id: str, kind: str, body: Dict[str, Any],
                       response: Dict[str, Any]) -> None:
        from .turns import body_hash
        self.store.insert("decisions", {"decision_id": decision_id, "target_id": target_id, "kind": kind,
                                        "body_hash": body_hash(body), "response_json": dumps(response),
                                        "created_at": timeutil.iso()}, or_ignore=True)

    def _pending(self, chat_id: str, interaction_id: str, kind: str) -> Dict[str, Any]:
        from .turns import ApiError
        row = self.store.interaction(interaction_id)
        if row is None or row["chat_id"] != chat_id:
            raise ApiError(404, "not_found", "interaction not found")
        if row["kind"] != kind:
            raise ApiError(409, "invalid_state", f"interaction is not {'a question' if kind == 'input' else 'an approval'}")
        if row["state"] == "expired" or (row["expires_at"] and timeutil.parse(row["expires_at"]) < timeutil.now()):
            raise ApiError(409, "expired", "interaction expired")
        if row["state"] not in OPEN_STATES:
            raise ApiError(409, "already_decided", "interaction already decided")
        return row

    async def answer(self, chat_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        from .turns import ApiError
        interaction_id = body["interaction_id"]
        duplicate = self.check_decision(body["decision_id"], interaction_id, "answer", body)
        if duplicate:
            return duplicate
        row = self._pending(chat_id, interaction_id, "input")
        question = loads(row["question_json"], {}) or {}
        labels = {o["id"]: o["label"] for o in question.get("options", [])}
        if body.get("option_ids"):
            unknown = [o for o in body["option_ids"] if o not in labels]
            if unknown:
                raise ApiError(400, "invalid_request", "unknown option id")
            chosen = [labels[o] for o in body["option_ids"]]
            value: Any = chosen if question.get("multi_select") else chosen[0]
        else:
            value = jsonlog.redact_text(body["text"])
        task = self.store.task(row["task_id"])
        run = self.engine.current_run(row["chat_id"])
        future = run.futures.get(interaction_id) if run else None
        self._mark_answered(row, value, body["via"], body["decision_id"])
        if future is not None and not future.done():
            future.set_result(("answer", value))
        elif task is not None and task["state"] not in TERMINAL_STATES:
            text = value if isinstance(value, str) else ", ".join(value)
            await self.engine.answer_turn(task, f'[answer to your earlier question "{question.get("text", "")}"]: {text}',
                                          task["lang"], task["trace_id"], None)
        response = {"interaction": self.public(self.store.interaction(interaction_id)), "duplicate": False}
        self.store_decision(body["decision_id"], interaction_id, "answer", body, response)
        return response

    async def decide(self, chat_id: str, body: Dict[str, Any], decision: str) -> Dict[str, Any]:
        interaction_id = body["interaction_id"]
        duplicate = self.check_decision(body["decision_id"], interaction_id, decision, body)
        if duplicate:
            return duplicate
        row = self._pending(chat_id, interaction_id, "approval")
        await self._decide_approval(row, decision, body["via"])
        response = {"interaction": self.public(self.store.interaction(interaction_id)), "duplicate": False}
        self.store_decision(body["decision_id"], interaction_id, decision, body, response)
        return response

    async def _decide_approval(self, row: Dict[str, Any], decision: str, via: str) -> None:
        from .turns import ApiError
        if decision == "approve" and via == "voice" and not row["voice_can_approve"]:
            raise ApiError(409, "invalid_state", "this action needs a tap on the panel")
        if row["source"] == "ha_mcp":
            try:
                await self.engine.ha.decide(row["external_ref"], decision, via, row["device_id"])
            except Uncorrelated:
                raise ApiError(409, "invalid_state", "not provably ours; decide it in the Home Assistant panel")
            except NotOwner:
                raise ApiError(409, "invalid_state", "held for another device")
            except AlreadyDecided:
                raise ApiError(409, "already_decided", "already decided")
            approval = self.store.approval(row["external_ref"])
            if approval and approval["tool_use_id"]:
                self.store.update("tool_calls", "tool_use_id", approval["tool_use_id"], {
                    "approval_decision": decision, "decided_via": via, "decided_by_device": row["device_id"],
                    "decided_at": timeutil.iso()})
        state = "approved" if decision == "approve" else "denied"
        current = self.store.interaction(row["interaction_id"])
        if current["state"] in OPEN_STATES:
            self.store.update("interactions", "interaction_id", row["interaction_id"], {
                "state": state, "decision_json": {"decision": decision}, "via": via,
                "decided_at": timeutil.iso()})
            self.resolved(row, state, via)
        run = self.engine.current_run(row["chat_id"])
        future = run.futures.get(row["interaction_id"]) if run else None
        if future is not None and not future.done():
            future.set_result((decision, None))

    # ------------------------------------------------------------------ cancel and close

    async def withdraw_task(self, task_id: str) -> None:
        task = self.store.task(task_id)
        for row in self.open_for_task(task_id):
            if row["source"] == "ha_mcp":
                outcome = await self.engine.ha.withdraw(row["external_ref"])
                if outcome == "uncorrelated":
                    self.store.update("interactions", "interaction_id", row["interaction_id"], {"state": "uncorrelated"})
                    self.card(row, note=UNCORRELATED_TEXT[task["lang"] if task else "en"])
                    continue
            self.store.update("interactions", "interaction_id", row["interaction_id"],
                              {"state": "cancelled", "decided_at": timeutil.iso()})
            self.resolved(row, "cancelled", None)

    def close_task_interactions(self, task: Dict[str, Any]) -> None:
        for row in self.open_for_task(task["task_id"]):
            if row["source"] == "ha_mcp" and row["state"] == "uncorrelated":
                continue
            self.store.update("interactions", "interaction_id", row["interaction_id"],
                              {"state": "cancelled", "decided_at": timeutil.iso()})
            self.outbox.withdraw(row["device_id"], f"int:{row['interaction_id']}")

    # ------------------------------------------------------------------ HA approval listener

    def on_pending(self, approval: Dict[str, Any], waiter: Dict[str, Any]) -> None:
        task = self.store.task(waiter.get("task_id"))
        if task is None or task["state"] in TERMINAL_STATES:
            return
        exists = self.store.one("SELECT 1 FROM interactions WHERE external_ref = ? AND task_id = ?",
                                (approval["approval_id"], task["task_id"]))
        if exists:
            return
        args = loads(approval["args_json"], {})
        text, target = describe_call(approval["tool"], args)
        can_voice = reversible(approval["tool"], args)
        action = {"text": text, "detail": jsonlog.truncate(f"{approval['tool']} {target or ''}".strip(), 120),
                  "tool": approval["tool"], "risk": "low" if can_voice else "high", "reversible": can_voice}
        ttl = self.engine.ha.approval_ttl_s
        owned = self.engine.ha.owned(approval)
        row = self._new(task["task_id"], "approval", "ha_mcp", ttl, action=action, external_ref=approval["approval_id"],
                        tool_use_id=waiter.get("tool_use_id"), voice_can_approve=can_voice and owned,
                        expires_at=approval["ha_expires_at"], turn_id=waiter.get("turn_id"),
                        state="pending" if owned else "uncorrelated")
        if waiter.get("tool_use_id"):
            self.store.update("tool_calls", "tool_use_id", waiter["tool_use_id"],
                              {"status": "pending", "approval_id": approval["approval_id"]})
        run = self.engine.current_run(task["chat_id"])
        if run is not None and run.turn_id != waiter.get("turn_id"):
            run = None
        self.announce(row, run)

    def on_correlated(self, approval: Dict[str, Any]) -> None:
        """Our own request returned the hold's token: its cards become actionable; a hold whose tasks all ended
        (cancelled meanwhile) is denied instead."""
        if not self.engine.ha.owned(approval):
            return
        tasks = [self.store.task(w.get("task_id")) for w in loads(approval["waiters_json"], [])]
        if tasks and all(t is None or t["state"] in TERMINAL_STATES for t in tasks):
            self.engine.spawn(self.engine.ha.withdraw(approval["approval_id"], via="cancelled"),
                              f"withdraw:{approval['approval_id']}")
            return
        for row in self.store.all("SELECT * FROM interactions WHERE external_ref = ? AND state IN ('pending', 'uncorrelated')",
                                  (approval["approval_id"],)):
            action = loads(row["action_json"], {}) or {}
            values = {"state": "pending", "voice_can_approve": int(bool(action.get("reversible")))}
            if approval["ha_expires_at"]:
                values["expires_at"] = approval["ha_expires_at"]
            self.store.update("interactions", "interaction_id", row["interaction_id"], values)
            task = self.store.task(row["task_id"])
            live = task is not None and task["state"] not in TERMINAL_STATES
            self.card(row, speak=self._speak_text(self.store.interaction(row["interaction_id"]), task["lang"])
                      if live else None)
        if approval["tool_use_id"]:
            self.store.update("tool_calls", "tool_use_id", approval["tool_use_id"], {"ha_token_id": approval["approval_id"]})

    def on_terminal(self, approval: Dict[str, Any]) -> None:
        state = approval["state"]
        status = {"executed": "ok", "failed": "error", "not_sent": "not_run", "denied": "denied",
                  "expired": "expired"}.get(state, "unknown")
        text, _ = describe_call(approval["tool"], loads(approval["args_json"], {}))
        waiters = loads(approval["waiters_json"], [])
        for waiter in waiters:
            if waiter.get("tool_use_id"):
                self.store.update("tool_calls", "tool_use_id", waiter["tool_use_id"], {
                    "status": status, "ended_at": timeutil.iso(),
                    "result_summary": jsonlog.truncate(approval["result_text"] or state, 200),
                    "approval_id": approval["approval_id"], "approval_decision": approval["decision"],
                    "decided_via": approval["via"], "decided_at": approval["decided_at"]})
        interaction_state = {"executed": "approved", "failed": "approved", "denied": "denied"}.get(state, "expired")
        for row in self.store.all("SELECT * FROM interactions WHERE external_ref = ?", (approval["approval_id"],)):
            if row["state"] in OPEN_STATES:
                self.store.update("interactions", "interaction_id", row["interaction_id"], {"state": interaction_state})
                self.resolved(row, interaction_state, row["via"] or approval["via"])
        outcome = {"executed": "executed", "failed": f"failed: {approval['result_text'] or ''}", "denied": "denied",
                   "expired": "expired", "not_sent": "not sent"}.get(state, "result unknown")
        line = (f"[event approval {approval['approval_id']}] {approval['tool']} {text}: "
                f"{jsonlog.truncate(outcome, 300)}. Already handled; do not call the tool again.")
        chats = []
        for waiter in waiters:
            if waiter.get("chat_id") and waiter["chat_id"] not in chats:
                chats.append(waiter["chat_id"])
        for chat_id in chats:
            self.chats.add_note(chat_id, "event", line)
            self.engine.schedule_event_turn(chat_id)

