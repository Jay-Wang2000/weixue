"""Chat index: one transcript per chat, current/previous chat per device, context notes."""
import difflib
from typing import Any, Callable, Dict, List, Optional

from . import jsonlog, timeutil
from .config import SUMMARY_MAX, TITLE_MAX
from .ids import new_id
from .store import TERMINAL_STATES, Store

PLACEHOLDER_TITLE = "New chat"


class ChatIndex:
    def __init__(self, store: Store, on_change: Optional[Callable[[str], None]] = None,
                 in_flight: Optional[Callable[[str], bool]] = None):
        self.store = store
        self.on_change = on_change
        self.in_flight = in_flight or (lambda chat_id: False)

    def _changed(self, device_id: str) -> None:
        if self.on_change:
            self.on_change(device_id)

    def create(self, device_id: str, make_current: bool = True) -> Dict[str, Any]:
        self.store.device(device_id)
        now = timeutil.iso()
        chat_id = new_id("cht")
        self.store.insert("chats", {
            "chat_id": chat_id, "device_id": device_id,
            "title": PLACEHOLDER_TITLE, "title_source": "placeholder", "created_at": now, "last_activity_at": now,
        })
        if make_current:
            self.switch(device_id, chat_id)
        jsonlog.log("chat created", device_id=device_id, chat_id=chat_id, event="chat_created")
        return self.store.chat(chat_id)

    def current(self, device_id: str) -> Dict[str, Any]:
        device = self.store.device(device_id)
        if device["current_chat_id"]:
            chat = self.store.chat(device["current_chat_id"])
            if chat:
                return chat
        return self.create(device_id)

    def switch(self, device_id: str, chat_id: str) -> Dict[str, Any]:
        """Makes chat_id current; a spoken interaction window of another chat closes."""
        device = self.store.device(device_id)
        previous = device["current_chat_id"]
        values = {"current_chat_id": chat_id, "updated_at": timeutil.iso()}
        if previous and previous != chat_id:
            values["previous_chat_id"] = previous
        if previous != chat_id and device["voice_window_until"]:
            row = self.store.interaction(device["voice_interaction_id"]) if device["voice_interaction_id"] else None
            if row is None or row["chat_id"] != chat_id:
                values["voice_window_until"] = None
        self.store.update("devices", "device_id", device_id, values)
        self._changed(device_id)
        return self.store.device(device_id)

    def back(self, device_id: str) -> Optional[Dict[str, Any]]:
        device = self.store.device(device_id)
        if not device["previous_chat_id"]:
            return None
        return self.switch(device_id, device["previous_chat_id"])

    def touch(self, chat_id: str, first_text: Optional[str] = None) -> None:
        chat = self.store.chat(chat_id)
        values: Dict[str, Any] = {"last_activity_at": timeutil.iso(), "activity_seq": chat["activity_seq"] + 1}
        if first_text and chat["title_source"] == "placeholder":
            values.update(title=jsonlog.truncate(first_text, TITLE_MAX), title_source="first_utterance")
        self.store.update("chats", "chat_id", chat_id, values)
        self._changed(chat["device_id"])

    def set_title(self, chat_id: str, title: str) -> None:
        chat = self.store.chat(chat_id)
        self.store.update("chats", "chat_id", chat_id, {"title": jsonlog.truncate(title, TITLE_MAX), "title_source": "agent"})
        self._changed(chat["device_id"])

    def set_summary(self, chat_id: str, summary: Optional[str]) -> None:
        if summary:
            self.store.update("chats", "chat_id", chat_id, {"summary": jsonlog.truncate(summary, SUMMARY_MAX)})

    def mark_read(self, chat_id: str) -> None:
        chat = self.store.chat(chat_id)
        self.store.update("chats", "chat_id", chat_id, {"last_read_seq": chat["activity_seq"]})
        self._changed(chat["device_id"])

    # ------------------------------------------------------------------ context notes for the next turn

    def add_note(self, chat_id: str, kind: str, text: str) -> None:
        self.store.insert("chat_notes", {"chat_id": chat_id, "kind": kind, "text": text, "created_at": timeutil.iso()})

    def pending_notes(self, chat_id: str, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM chat_notes WHERE chat_id = ? AND consumed_by IS NULL"
        params: List[Any] = [chat_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        return self.store.all(sql + " ORDER BY id", params)

    def take_notes(self, chat_id: str, turn_id: str, kind: Optional[str] = None) -> List[str]:
        rows = self.pending_notes(chat_id, kind)
        for row in rows:
            self.store.update("chat_notes", "id", row["id"], {"consumed_by": turn_id})
        return [row["text"] for row in rows]

    # ------------------------------------------------------------------ views

    def state_of(self, chat: Dict[str, Any]) -> Dict[str, Any]:
        task = self.store.task(chat["open_task_id"]) if chat["open_task_id"] else None
        if task is not None and task["state"] in TERMINAL_STATES:
            task = None
        last = self.store.one("SELECT state FROM tasks WHERE chat_id = ? ORDER BY created_at DESC LIMIT 1",
                              (chat["chat_id"],))
        unread = chat["activity_seq"] > chat["last_read_seq"]
        turn_in_flight = self.in_flight(chat["chat_id"])
        if task is not None and task["state"] in ("needs_input", "needs_approval"):
            state = "needs_you"
        elif task is not None or turn_in_flight:
            state = "working"
        elif last and last["state"] == "failed":
            state = "failed"
        elif unread:
            state = "unread"
        else:
            state = "idle"
        return {"state": state, "unread": unread, "open_task_id": task["task_id"] if task else None,
                "turn_in_flight": turn_in_flight}

    def public(self, chat: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "chat_id": chat["chat_id"], "title": chat["title"], "title_source": chat["title_source"],
            "summary": chat["summary"], "last_activity_at": chat["last_activity_at"], "created_at": chat["created_at"],
            **self.state_of(chat),
        }

    def listing(self, device_id: str, limit: int = 12) -> Dict[str, Any]:
        device = self.store.device(device_id)
        rows = self.store.all("SELECT * FROM chats WHERE device_id = ? ORDER BY last_activity_at DESC LIMIT ?",
                              (device_id, limit))
        current = device["current_chat_id"]
        rows.sort(key=lambda c: c["chat_id"] != current)
        return {"device_id": device_id, "current_chat_id": current, "previous_chat_id": device["previous_chat_id"],
                "items": [self.public(c) for c in rows]}

    def resolve(self, device_id: str, query: str) -> List[Dict[str, Any]]:
        rows = self.store.all("SELECT chat_id, title, summary FROM chats WHERE device_id = ?", (device_id,))
        q = query.lower().strip()
        scored = []
        for row in rows:
            haystack = f"{row['title'] or ''} {row['summary'] or ''}".lower()
            score = difflib.SequenceMatcher(None, q, (row["title"] or "").lower()).ratio()
            if q and q in haystack:
                score = max(score, 0.9)
            scored.append({"chat_id": row["chat_id"], "title": row["title"], "score": round(score, 3)})
        scored.sort(key=lambda r: -r["score"])
        return scored[:3]
