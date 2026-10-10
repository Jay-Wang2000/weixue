"""A fake streaming Messages API at the HTTP layer, so the real SDK and its tool runner run unchanged.

A script is `async def script(turn) -> None`. `turn` streams blocks of the current response and calls tools:
`await turn.tool(name, input)` ends the response with `tool_use` and returns once the next request brings the
result (`turn.steer_text` / `turn.notes` then hold the user text and system notes sent with it); `await
turn.pause()` ends it with `pause_turn` and returns when the paused message is sent back. When the script
returns, the response ends with `turn.stop_reason` (default `end_turn`). A request that continues no script
starts a new one. A client closing the stream cancels the script (`api.cancelled` counts it).
"""
import asyncio
import itertools
import json
from typing import Any, Callable, Dict, List, Optional

import anthropic
import httpx2

_ids = itertools.count(1)


def _sse(event: Dict[str, Any]) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


def last_user(body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for message in reversed(body["messages"]):
        if message["role"] == "user":
            return message
    return None


def blocks(message: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if message is None:
        return []
    content = message["content"]
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content)


def texts(message: Optional[Dict[str, Any]]) -> str:
    return "\n".join(b["text"] for b in blocks(message) if b.get("type") == "text")


def system_notes(body: Dict[str, Any]) -> List[str]:
    """Text of the system messages after the last user message."""
    notes: List[str] = []
    for message in reversed(body["messages"]):
        if message["role"] == "user":
            break
        if message["role"] == "system":
            notes[:0] = [b["text"] for b in blocks(message) if b.get("type") == "text"]
    return notes


class Stream(httpx2.AsyncByteStream):
    def __init__(self, response: "Response"):
        self.response = response

    async def __aiter__(self):
        while True:
            chunk = await self.response.queue.get()
            if chunk is None:
                return
            yield chunk

    async def aclose(self) -> None:
        self.response.closed()


class Response:
    def __init__(self, turn: "Turn", body: Dict[str, Any]):
        self.turn = turn
        self.body = body
        self.queue: asyncio.Queue = asyncio.Queue()
        self.index = 0
        self.content: List[Dict[str, Any]] = []
        self.done = False
        self.started = False

    def put(self, event: Dict[str, Any]) -> None:
        if not self.started:
            self.started = True
            usage = dict(self.turn.api.usage)
            self.queue.put_nowait(_sse({"type": "message_start", "message": {
                "id": f"msg_{next(_ids):06d}", "type": "message", "role": "assistant", "model": self.body["model"],
                "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage,
                "input_transformations": self.turn.api.transformations}}))
        self.queue.put_nowait(_sse(event))

    def block(self, start: Dict[str, Any], deltas: List[Dict[str, Any]]) -> None:
        self.put({"type": "content_block_start", "index": self.index, "content_block": start})
        for delta in deltas:
            self.put({"type": "content_block_delta", "index": self.index, "delta": delta})
        self.put({"type": "content_block_stop", "index": self.index})
        self.index += 1

    def finish(self, stop_reason: str) -> None:
        if self.done:
            return
        self.done = True
        self.put({"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                  "usage": {"output_tokens": 10}})
        self.put({"type": "message_stop"})
        self.queue.put_nowait(None)

    def closed(self) -> None:
        if not self.done:
            self.done = True
            self.turn.api.cancelled += 1
            self.turn.api.cancel_times.append(asyncio.get_running_loop().time())
            if self.turn.task is not None and not self.turn.task.done():
                self.turn.task.cancel()


class Turn:
    def __init__(self, api: "FakeApi", body: Dict[str, Any]):
        self.api = api
        self.requests: List[Dict[str, Any]] = [body]
        self.response = Response(self, body)
        self.task: Optional[asyncio.Task] = None
        self.stop_reason = "end_turn"
        self.steer_text = ""
        self.notes: List[str] = []
        self._next: Optional[asyncio.Future] = None

    @property
    def body(self) -> Dict[str, Any]:
        return self.requests[-1]

    @property
    def model(self) -> str:
        return self.body["model"]

    @property
    def user_text(self) -> str:
        return texts(last_user(self.body))

    @property
    def prompt(self) -> str:
        """The system notes after the last user message, then that message's text (latest request)."""
        return "\n".join(system_notes(self.body) + [self.user_text])

    @property
    def first_notes(self) -> List[str]:
        return system_notes(self.requests[0])

    async def text(self, text: str, delay: float = 0.0) -> None:
        words = text.split(" ")
        self.response.put({"type": "content_block_start", "index": self.response.index,
                           "content_block": {"type": "text", "text": ""}})
        for i, word in enumerate(words):
            self.response.put({"type": "content_block_delta", "index": self.response.index,
                               "delta": {"type": "text_delta", "text": word + (" " if i < len(words) - 1 else "")}})
            if delay:
                await asyncio.sleep(delay)
        self.response.put({"type": "content_block_stop", "index": self.response.index})
        self.response.index += 1
        await asyncio.sleep(0)

    def thinking(self, note: str = "") -> None:
        self.response.block({"type": "thinking", "thinking": "", "signature": ""},
                            ([{"type": "thinking_delta", "thinking": note}] if note else [])
                            + [{"type": "signature_delta", "signature": f"sig{next(_ids)}"}])

    def server_tool(self, name: str = "web_search", query: str = "weather") -> None:
        tool_id = f"srvtoolu_{next(_ids):06d}"
        self.response.block({"type": "server_tool_use", "id": tool_id, "name": name, "input": {}},
                            [{"type": "input_json_delta", "partial_json": json.dumps({"query": query})}])
        self.response.block({"type": "web_search_tool_result", "tool_use_id": tool_id, "content": [
            {"type": "web_search_result", "title": "Weather", "url": "https://example.test/w",
             "encrypted_content": "e", "page_age": None}]}, [])

    def server_tool_unpaired(self, name: str = "tool_search_tool_bm25") -> None:
        """A server tool use whose result the response does not carry (as Fable 5.1 sends next to a client call)."""
        self.response.block({"type": "server_tool_use", "id": f"srvtoolu_{next(_ids):06d}", "name": name, "input": {}},
                            [{"type": "input_json_delta", "partial_json": json.dumps({"query": "automation"})}])

    async def _wait_next(self, keys: List[str]) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        self._next = loop.create_future()
        for key in keys:
            self.api.waiting[key] = self
        body = await self._next
        return body

    def resume(self, body: Dict[str, Any]) -> Response:
        self.requests.append(body)
        self.response = Response(self, body)
        if self._next is not None and not self._next.done():
            self._next.set_result(body)
        return self.response

    async def tool(self, name: str, input: Dict[str, Any]) -> str:
        return (await self.tools([(name, input)]))[0]

    async def tools(self, calls: List[Any]) -> List[str]:
        ids = []
        for name, input in calls:
            tool_id = f"toolu_{next(_ids):06d}"
            ids.append(tool_id)
            self.response.block({"type": "tool_use", "id": tool_id, "name": name, "input": {}},
                                [{"type": "input_json_delta", "partial_json": json.dumps(input)}])
        self.response.finish("tool_use")
        body = await self._wait_next(ids)
        user = last_user(body)
        results = {b["tool_use_id"]: b for b in blocks(user) if b.get("type") == "tool_result"}
        self.steer_text = "\n".join(b["text"] for b in blocks(user) if b.get("type") == "text")
        self.notes = system_notes(body)
        out = []
        for tool_id in ids:
            content = results.get(tool_id, {}).get("content")
            if isinstance(content, list):
                content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
            out.append(content or "")
        return out

    async def ask(self, question: str, options: List[str]) -> str:
        return await self.tool("ask_user", {"question": question, "options": options})

    async def pause(self) -> None:
        tool_id = f"srvtoolu_{next(_ids):06d}"
        self.response.block({"type": "server_tool_use", "id": tool_id, "name": "web_search", "input": {}},
                            [{"type": "input_json_delta", "partial_json": json.dumps({"query": "long"})}])
        self.response.finish("pause_turn")
        await self._wait_next([tool_id])


class FakeApi:
    def __init__(self, script: Callable[[Turn], Any]):
        self.script = script
        self.requests: List[Dict[str, Any]] = []
        self.waiting: Dict[str, Turn] = {}
        self.turns: List[Turn] = []
        self.cancelled = 0
        self.cancel_times: List[float] = []
        self.usage: Dict[str, Any] = {"input_tokens": 100, "output_tokens": 0, "cache_read_input_tokens": 0,
                                      "cache_creation_input_tokens": 0}
        self.transformations: List[Dict[str, Any]] = []
        self.fail_next: Optional[httpx2.Response] = None
        self.reject_models: Dict[str, Callable[[], httpx2.Response]] = {}
        self.compactions = 0
        self.errors: List[BaseException] = []

    @property
    def prompts(self) -> List[str]:
        """Per request: the system notes after the last user message, then that message's text."""
        return ["\n".join(system_notes(b) + [texts(last_user(b))]) for b in self.requests if not b.get("compaction")]

    def close(self) -> None:
        for turn in self.turns:
            if turn.task is not None and not turn.task.done():
                turn.task.cancel()

    def client(self) -> anthropic.AsyncAnthropic:
        return anthropic.AsyncAnthropic(api_key="sk-ant-test-key-0000000000", base_url="http://fake-api",
                                        max_retries=0, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(
                                            self.handle)))

    def _continues(self, body: Dict[str, Any]) -> Optional[Turn]:
        messages = body["messages"]
        user = last_user(body)
        for block in blocks(user):
            if block.get("type") == "tool_result" and block.get("tool_use_id") in self.waiting:
                return self.waiting.pop(block["tool_use_id"])
        for message in reversed(messages):
            if message["role"] == "assistant":
                for block in blocks(message):
                    if block.get("id") in self.waiting and block.get("type") == "server_tool_use":
                        return self.waiting.pop(block["id"])
                break
        return None

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if self.fail_next is not None:
            response, self.fail_next = self.fail_next, None
            return response
        if body["model"] in self.reject_models:
            return self.reject_models[body["model"]]()
        headers = {"content-type": "text/event-stream", "request-id": f"req_{next(_ids):06d}"}
        if body.get("compaction"):
            self.compactions += 1
            turn = Turn(self, body)
            turn.response.block({"type": "compaction", "content": ""},
                                [{"type": "compaction_delta", "content": "Summary of the conversation."}])
            turn.response.finish("compaction")
            return httpx2.Response(200, headers=headers, stream=Stream(turn.response))
        turn = self._continues(body)
        if turn is not None:
            response = turn.resume(body)
        else:
            turn = Turn(self, body)
            self.turns.append(turn)
            response = turn.response
            turn.task = asyncio.get_running_loop().create_task(self._run(turn))
        return httpx2.Response(200, headers=headers, stream=Stream(response))

    async def _run(self, turn: Turn) -> None:
        try:
            await self.script(turn)
        except asyncio.CancelledError:
            return
        except Exception as e:
            self.errors.append(e)
            turn.response.finish("end_turn")
            raise
        turn.response.finish(turn.stop_reason)


def error_response(status: int, message: str, kind: str = "invalid_request_error") -> httpx2.Response:
    return httpx2.Response(status, json={"type": "error", "error": {"type": kind, "message": message}},
                           headers={"content-type": "application/json"})
