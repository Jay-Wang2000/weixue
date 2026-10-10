"""Append-only transcript of one chat: the sole writer of `transcript` rows.

A request sends the current epoch's messages in `seq` order. Rows a request sent stay `tail` until its
response completes; only that uncommitted tail may be rewritten (the cancel path), because no thinking
block was minted after it. A compaction message starts a new epoch.
"""
from typing import Any, Dict, Iterable, List, Optional, Set

from . import timeutil
from .store import Store, dumps, loads

FALLBACK_DROPPED = {"thinking", "redacted_thinking", "tool_use"}


def echo_form(content: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Content of an assistant message as it may be sent back after a mid-output fallback: before the last
    `fallback` block only text and paired server-tool blocks stay."""
    last = max((i for i, b in enumerate(content) if b.get("type") == "fallback"), default=None)
    if last is None:
        return content
    paired = {b.get("tool_use_id") for b in content if str(b.get("type", "")).endswith("_tool_result")}
    kept = []
    for i, block in enumerate(content):
        kind = block.get("type")
        if i < last and (kind in FALLBACK_DROPPED or (kind == "server_tool_use" and block.get("id") not in paired)):
            continue
        kept.append(block)
    return kept


def without_unpaired_server_tools(content: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A finished (not paused) assistant message without server tool uses whose result it does not carry: the API
    rejects such a block once the next user message holds text."""
    paired = {b.get("tool_use_id") for b in content if str(b.get("type", "")).endswith("_tool_result")}
    return [b for b in content if b.get("type") != "server_tool_use" or b.get("id") in paired]


def user_blocks(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content or [])


class Transcript:
    def __init__(self, store: Store, chat_id: str):
        self.store = store
        self.chat_id = chat_id

    @property
    def epoch(self) -> int:
        return int(self.store.scalar("SELECT epoch FROM chats WHERE chat_id = ?", (self.chat_id,)) or 0)

    def rows(self, epoch: Optional[int] = None) -> List[Dict[str, Any]]:
        return self.store.all("SELECT * FROM transcript WHERE chat_id = ? AND epoch = ? ORDER BY seq",
                              (self.chat_id, self.epoch if epoch is None else epoch))

    def messages(self) -> List[Dict[str, Any]]:
        return [loads(r["content_json"]) for r in self.rows()]

    def tail(self) -> List[Dict[str, Any]]:
        return [r for r in self.rows() if r["state"] == "tail"]

    def append(self, messages: Iterable[Dict[str, Any]], turn_id: Optional[str], state: str = "tail",
               model: Optional[str] = None, request_id: Optional[str] = None, rewritten: int = 0) -> None:
        epoch = self.epoch
        now = timeutil.iso()
        with self.store.tx() as db:
            seq = db.execute("SELECT COALESCE(MAX(seq), -1) FROM transcript WHERE chat_id = ? AND epoch = ?",
                             (self.chat_id, epoch)).fetchone()[0]
            for message in messages:
                seq += 1
                db.execute(
                    "INSERT INTO transcript (chat_id, epoch, seq, role, content_json, model, turn_id, request_id, "
                    "state, rewritten, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.chat_id, epoch, seq, message["role"], dumps(message), model, turn_id, request_id, state,
                     rewritten, now))

    def commit(self) -> int:
        """The response to the rows sent so far completed: they can no longer be rewritten."""
        return self.store.execute("UPDATE transcript SET state = 'committed' WHERE chat_id = ? AND epoch = ? "
                                  "AND state = 'tail'", (self.chat_id, self.epoch))

    def rewrite_tail(self, messages: List[Dict[str, Any]], turn_id: Optional[str]) -> None:
        """Replace the uncommitted tail by `messages` (cancel path)."""
        tail = self.tail()
        if not tail:
            raise ValueError("no uncommitted tail to rewrite")
        rewritten = max(r["rewritten"] for r in tail) + 1
        with self.store.tx() as db:
            db.execute("DELETE FROM transcript WHERE chat_id = ? AND epoch = ? AND state = 'tail'",
                       (self.chat_id, self.epoch))
            self.append(messages, turn_id, state="tail", rewritten=rewritten)

    def new_epoch(self, compaction_message: Dict[str, Any], turn_id: Optional[str], model: Optional[str],
                  request_id: Optional[str]) -> int:
        epoch = self.epoch + 1
        with self.store.tx():
            self.store.update("chats", "chat_id", self.chat_id, {"epoch": epoch})
            self.append([compaction_message], turn_id, state="committed", model=model, request_id=request_id)
        return epoch

    def open_tool_uses(self) -> List[Dict[str, Any]]:
        """tool_use blocks of the epoch that no later user message answers."""
        answered: Set[str] = set()
        uses: List[Dict[str, Any]] = []
        for message in self.messages():
            if message["role"] == "assistant":
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        uses.append(block)
            elif message["role"] == "user":
                for block in user_blocks(message):
                    if block.get("type") == "tool_result":
                        answered.add(block.get("tool_use_id"))
        return [u for u in uses if u.get("id") not in answered]
