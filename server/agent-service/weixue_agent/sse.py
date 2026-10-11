"""Per-chat event log: persisted envelopes, live fan-out, SSE framing and text coalescing."""
import asyncio
import json
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Set

from . import timeutil
from .clause import first_clause_cut
from .store import Store

TERMINAL_EVENTS = frozenset({"done", "failed", "cancelled"})
SENTENCE_ENDS = (".", "!", "?", "。", "！", "？", "\n")
HEARTBEAT_FRAME = ": hb\n\n"


def envelope(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "v": 1, "seq": row["seq"], "chat_id": row["chat_id"], "task_id": row["task_id"], "turn_id": row["turn_id"],
        "trace_id": row["trace_id"], "ts": row["ts"], "type": row["type"], "data": json.loads(row["data_json"]),
    }


def sse_frame(event: Dict[str, Any]) -> str:
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {data}\n\n"


class EventHub:
    def __init__(self, store: Store):
        self.store = store
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}

    def append(self, chat_id: str, type_: str, data: Dict[str, Any], trace_id: str, task_id: Optional[str] = None,
               turn_id: Optional[str] = None) -> Dict[str, Any]:
        ts = timeutil.iso()
        seq = self.store.append_event(chat_id, type_, data, ts, task_id, turn_id, trace_id)
        event = {"v": 1, "seq": seq, "chat_id": chat_id, "task_id": task_id, "turn_id": turn_id,
                 "trace_id": trace_id, "ts": ts, "type": type_, "data": data}
        for queue in list(self._subscribers.get(chat_id, ())):
            queue.put_nowait(event)
        return event

    def replay(self, chat_id: str, after: int) -> List[Dict[str, Any]]:
        return [envelope(row) for row in self.store.events_after(chat_id, after)]

    async def stream(self, chat_id: str, after: int, until: Callable[[Dict[str, Any]], bool],
                     heartbeat_s: Callable[[], float]) -> AsyncIterator[Optional[Dict[str, Any]]]:
        """Events after `after`, then live ones, up to and including the first one `until` accepts.

        None marks a heartbeat."""
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(chat_id, set()).add(queue)
        try:
            last = after
            for event in self.replay(chat_id, after):
                last = event["seq"]
                yield event
                if until(event):
                    return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=heartbeat_s())
                except asyncio.TimeoutError:
                    yield None
                    continue
                if event["seq"] <= last:
                    continue
                last = event["seq"]
                yield event
                if until(event):
                    return
        finally:
            subscribers = self._subscribers.get(chat_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(chat_id, None)


class TextCoalescer:
    """Buffers streamed text deltas into one `emit(text)` per interval or sentence end; every delta up to the end
    of the first clause (clause.first_clause_cut) goes out at once."""

    def __init__(self, emit: Callable[[str], Any], interval_s: float):
        self._emit = emit
        self._interval = interval_s
        self._buffer: List[str] = []
        self._timer: Optional[asyncio.TimerHandle] = None
        self._head: Optional[str] = ""

    def add(self, delta: str) -> None:
        self._buffer.append(delta)
        if self._head is not None:
            self._head += delta
            if first_clause_cut(self._head):
                self._head = None
            self.flush()
        elif delta.rstrip().endswith(SENTENCE_ENDS):
            self.flush()
        elif self._timer is None:
            self._timer = asyncio.get_running_loop().call_later(self._interval, self.flush)

    def discard(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._buffer = []

    def flush(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._buffer:
            text = "".join(self._buffer)
            self._buffer = []
            self._emit(text)
