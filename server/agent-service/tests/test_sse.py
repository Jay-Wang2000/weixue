"""SSE framing and replay."""
from weixue_agent.sse import sse_frame


def test_frame_format():
    frame = sse_frame({"seq": 3, "type": "text", "data": {"delta": "hi"}})
    lines = frame.split("\n")
    assert lines[0] == "id: 3" and lines[1] == "event: text" and lines[2].startswith("data: {")
    assert frame.endswith("\n\n")


async def test_message_stream_resumes_after_last_event_id(make):
    h = await make()
    _, events = await h.send("hello", key="k-sse")
    last = events[1]["seq"]
    resp = await h.http.post("/v1/chats/current/messages", json={"device_id": "sim-device-01", "text": "hello"},
                             headers={"Idempotency-Key": "k-sse", "Accept": "text/event-stream",
                                      "Last-Event-ID": str(last)})
    from conftest import parse_sse
    replay = parse_sse(resp.text)
    assert replay[0]["seq"] == last + 1 and replay[-1]["type"] == "reply_end"
    assert resp.headers["x-trace-id"]


async def test_coalescer_sends_the_first_delta_at_once_then_buffers():
    from weixue_agent.sse import TextCoalescer
    out = []
    c = TextCoalescer(out.append, 0.05)
    c.add("The")
    c.add(" living")
    c.add(" room")
    assert out == ["The"]
    c.add(" light is off.")
    assert out == ["The", " living room light is off."]
