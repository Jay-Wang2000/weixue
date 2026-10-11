"""SSE framing, replay and text coalescing."""
import asyncio

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


async def test_coalescer_sends_every_delta_of_the_first_clause_at_once_then_buffers():
    from weixue_agent.sse import TextCoalescer
    out = []
    c = TextCoalescer(out.append, 0.05)
    for delta in ("The", " living", " room", " light is off", ", and", " the", " heating"):
        c.add(delta)
    assert out == ["The", " living", " room", " light is off", ", and"]
    c.add(" is on.")
    assert out[-1] == " the heating is on."
    c.add(" Anything")
    c.add(" else")
    assert out[-1] == " the heating is on."
    await asyncio.sleep(0.1)
    assert out[-1] == " Anything else"


async def test_coalescer_first_clause_follows_the_tts_rule():
    from weixue_agent.sse import TextCoalescer
    zh, words, short = [], [], []
    c = TextCoalescer(zh.append, 3)
    for delta in ("好的，", "我来看", "看。", "然后", "告诉你"):
        c.add(delta)
    assert zh == ["好的，", "我来看", "看。"]
    c.discard()
    c = TextCoalescer(words.append, 3)
    for word in "one two three four five six seven eight nine ten".split():
        c.add(word + " ")
    assert words == [w + " " for w in "one two three four five six seven eight".split()]
    c.discard()
    c = TextCoalescer(short.append, 3)
    for delta in ("Dr. ", "Lee ", "at ", "3.", "5", " pm", ", then", " more"):
        c.add(delta)
    assert short == ["Dr. ", "Lee ", "at ", "3.", "5", " pm", ", then"]
    c.discard()
