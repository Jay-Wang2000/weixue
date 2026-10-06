"""Unit tests for core/providers/llm/anthropic (no network; fake SDK client)."""
import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from core.providers.llm.anthropic.anthropic import (
    DIRECT_ANSWER,
    MISSING_RESULT,
    REFUSAL_TEXT,
    TRUNCATED_TEXT,
    LLMProvider,
)
from core.utils.dialogue import Dialogue, Message

DIRECT_ANSWER_TOOL = {
    "type": "function",
    "function": {
        "name": DIRECT_ANSWER,
        "description": "direct reply",
        "parameters": {
            "type": "object",
            "properties": {"response": {"type": "string"}},
            "required": ["response"],
        },
    },
}
LIGHT_TOOL = {
    "type": "function",
    "function": {
        "name": "light_on",
        "description": "Turn a light on",
        "parameters": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
        },
    },
}
SONNET_CONFIG = {"api_key": "sk-test", "model_name": "claude-sonnet-5-5", "thinking": "between_tools"}


# ----------------------------------------------------------------- fakes


class FakeStream:
    def __init__(self, events, final):
        self.events = events
        self.final = final
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def __iter__(self):
        return iter(self.events)

    def get_final_message(self):
        return self.final


class FakeMessages:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls = []

    def stream(self, **params):
        self.calls.append(params)
        script = self.scripts.pop(0)
        if isinstance(script, BaseException):
            raise script
        return script


class FakeClient:
    def __init__(self, *scripts):
        self.messages = FakeMessages(scripts)


def ev_start(index, block):
    return SimpleNamespace(type="content_block_start", index=index, content_block=block)


def ev_text(index, text):
    return SimpleNamespace(
        type="content_block_delta", index=index, delta=SimpleNamespace(type="text_delta", text=text)
    )


def ev_json(index, partial):
    return SimpleNamespace(
        type="content_block_delta",
        index=index,
        delta=SimpleNamespace(type="input_json_delta", partial_json=partial),
    )


def ev_thinking(index, text=""):
    return SimpleNamespace(
        type="content_block_delta", index=index, delta=SimpleNamespace(type="thinking_delta", thinking=text)
    )


def ev_signature(index):
    return SimpleNamespace(
        type="content_block_delta", index=index, delta=SimpleNamespace(type="signature_delta", signature="sig")
    )


def ev_stop(index):
    return SimpleNamespace(type="content_block_stop", index=index)


def tool_block(tool_id, name):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input={})


def final_message(stop_reason="end_turn", category=None):
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category=category) if stop_reason == "refusal" else None,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0),
        _request_id="req_test",
    )


def api_error(cls, status):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(status, request=req), body={"type": "error"})


def make_provider(config=SONNET_CONFIG, *scripts):
    provider = LLMProvider(dict(config))
    provider.client = FakeClient(*scripts)
    return provider


def merge_tool_calls(tool_calls_list, tools_call):
    """Copy of ConnectionHandler._merge_tool_calls (core/connection.py)."""
    for tool_call in tools_call:
        tool_index = getattr(tool_call, "index", None)
        if tool_index is None:
            if tool_call.function.name:
                tool_index = len(tool_calls_list)
            else:
                tool_index = len(tool_calls_list) - 1 if tool_calls_list else 0
        if tool_index >= len(tool_calls_list):
            tool_calls_list.append({"id": "", "name": "", "arguments": ""})
        if tool_call.id:
            tool_calls_list[tool_index]["id"] = tool_call.id
        if tool_call.function.name:
            tool_calls_list[tool_index]["name"] = tool_call.function.name
        if tool_call.function.arguments:
            tool_calls_list[tool_index]["arguments"] += tool_call.function.arguments


def drive(provider, dialogue, functions):
    """Run response_with_functions through the upstream merge logic."""
    texts, calls, tool_yields = [], [], 0
    for text, tools in provider.response_with_functions("s", dialogue, functions=functions):
        if text:
            texts.append(text)
        if tools:
            tool_yields += 1
            merge_tool_calls(calls, tools)
    return texts, calls, tool_yields


def upstream_dialogue(*real_messages, with_fewshot=True):
    """Wire format as Dialogue.get_llm_dialogue_with_memory() produces it."""
    d = Dialogue()
    d.put(Message(role="system", content="SYSTEM {{current_time}}"))
    if with_fewshot:
        d.put(Message(role="user", content="给我讲个故事吧", is_temporary=True))
        d.put(
            Message(
                role="assistant",
                tool_calls=[
                    {
                        "id": "fewshot_da_001",
                        "function": {"arguments": '{"response": "好呀"}', "name": DIRECT_ANSWER},
                        "type": "function",
                        "index": 0,
                    }
                ],
                is_temporary=True,
            )
        )
        d.put(Message(role="tool", tool_call_id="fewshot_da_001", content="已直接回复", is_temporary=True))
    for m in real_messages:
        d.put(m)
    return d.get_llm_dialogue()


def blocks(msg, btype):
    return [b for b in msg["content"] if b["type"] == btype]


# ------------------------------------------------------- dialogue conversion


def test_system_split_and_basic_shapes():
    dialogue = upstream_dialogue(
        Message(role="user", content="turn on the light"),
        Message(
            role="assistant",
            tool_calls=[
                {"id": "toolu_1", "function": {"name": "light_on", "arguments": '{"entity_id": "light.a"}'}, "type": "function", "index": 0}
            ],
        ),
        Message(role="tool", tool_call_id="toolu_1", content="ok"),
        Message(role="user", content="thanks"),
        Message(role="user", content="   "),
    )
    system, rest = LLMProvider._split_system(dialogue)
    assert system.startswith("SYSTEM")
    msgs = LLMProvider._convert_dialogue(rest)

    assert msgs[0] == {"role": "user", "content": [{"type": "text", "text": "给我讲个故事吧"}]}
    assert blocks(msgs[1], "tool_use") == [
        {"type": "tool_use", "id": "fewshot_da_001", "name": DIRECT_ANSWER, "input": {"response": "好呀"}}
    ]
    assert msgs[2]["content"][0] == {"type": "tool_result", "tool_use_id": "fewshot_da_001", "content": "已直接回复"}
    assert msgs[2]["content"][1] == {"type": "text", "text": "turn on the light"}
    assert blocks(msgs[3], "tool_use")[0]["input"] == {"entity_id": "light.a"}
    assert msgs[4]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
        {"type": "text", "text": "thanks"},
    ]
    assert msgs[-1]["role"] == "user"
    for m in msgs:
        for b in m["content"]:
            assert b["type"] in ("text", "tool_use", "tool_result")
            if b["type"] == "text":
                assert b["text"].strip()


def test_interrupted_dummies_relocated_from_both_segments():
    d = Dialogue()
    d.put(Message(role="system", content="S"))
    # few-shot segment with a dangling tool call
    d.put(Message(role="user", content="拜拜", is_temporary=True))
    d.put(
        Message(
            role="assistant",
            tool_calls=[{"id": "fewshot_exit_001", "function": {"name": "handle_exit_intent", "arguments": "{}"}, "type": "function", "index": 0}],
            is_temporary=True,
        )
    )
    d.put(Message(role="assistant", content="再见", is_temporary=True))
    # real history with an interrupted call, then a new user turn
    d.put(Message(role="user", content="lights"))
    d.put(
        Message(
            role="assistant",
            tool_calls=[{"id": "toolu_x", "function": {"name": "light_on", "arguments": "{}"}, "type": "function", "index": 0}],
        )
    )
    d.put(Message(role="user", content="never mind"))
    wire = d.get_llm_dialogue()
    # upstream appends the dummies at the end of each segment
    assert wire[-1]["role"] == "tool" and wire[-1]["tool_call_id"] == "toolu_x"

    _, rest = LLMProvider._split_system(wire)
    msgs = LLMProvider._convert_dialogue(rest)

    def results_after(tool_id):
        for i, m in enumerate(msgs):
            if any(b.get("id") == tool_id for b in m["content"]):
                nxt = msgs[i + 1]
                assert nxt["role"] == "user"
                return [b for b in nxt["content"] if b["type"] == "tool_result" and b["tool_use_id"] == tool_id]
        raise AssertionError(tool_id)

    fs = results_after("fewshot_exit_001")
    assert len(fs) == 1 and "interrupted" in fs[0]["content"] and "is_error" not in fs[0]
    real = results_after("toolu_x")
    assert len(real) == 1 and "interrupted" in real[0]["content"] and "is_error" not in real[0]
    # the relocated result shares the user message with the next user text, results first
    idx = next(i for i, m in enumerate(msgs) if any(b.get("id") == "toolu_x" for b in m["content"]))
    assert [b["type"] for b in msgs[idx + 1]["content"]] == ["tool_result", "text"]
    assert msgs[-1]["role"] == "user"
    assert all(m["role"] != msgs[i + 1]["role"] for i, m in enumerate(msgs[:-1]))


def test_orphan_dropped_duplicate_dropped_missing_synthesized():
    rest = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [
            {"id": "toolu_a", "function": {"name": "a", "arguments": "not json"}, "type": "function", "index": 0},
            {"id": "toolu_b", "function": {"name": "b", "arguments": "{}"}, "type": "function", "index": 1},
        ]},
        {"role": "tool", "tool_call_id": "toolu_a", "content": "first"},
        {"role": "tool", "tool_call_id": "toolu_a", "content": "second"},
        {"role": "tool", "tool_call_id": "7a1c5c8e-0000-4000-8000-000000000000", "content": "orphan"},
        {"role": "user", "content": "and?"},
    ]
    msgs = LLMProvider._convert_dialogue(rest)
    assert blocks(msgs[1], "tool_use")[0]["input"] == {}
    results = blocks(msgs[2], "tool_result")
    assert [r["tool_use_id"] for r in results] == ["toolu_a", "toolu_b"]
    assert results[0]["content"] == "first" and "is_error" not in results[0]
    assert results[1] == {"type": "tool_result", "tool_use_id": "toolu_b", "is_error": True, "content": MISSING_RESULT}
    assert all("orphan" not in json.dumps(m) for m in msgs)
    assert msgs[2]["content"][-1] == {"type": "text", "text": "and?"}


def test_empty_tool_result_omits_content_and_text_then_tool_calls_merge():
    rest = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "On it."},
        {"role": "assistant", "tool_calls": [{"id": "toolu_1", "function": {"name": "a", "arguments": "{}"}, "type": "function", "index": 0}]},
        {"role": "tool", "tool_call_id": "toolu_1", "content": ""},
        {"role": "user", "content": "ok"},
    ]
    msgs = LLMProvider._convert_dialogue(rest)
    assert [b["type"] for b in msgs[1]["content"]] == ["text", "tool_use"]
    assert msgs[2]["content"][0] == {"type": "tool_result", "tool_use_id": "toolu_1"}


# -------------------------------------------------------------- tool schemas


def test_convert_tools():
    provider = make_provider()
    bad = {"type": "function", "function": {"name": "has space", "parameters": {}}}
    tools = provider._convert_tools([DIRECT_ANSWER_TOOL, LIGHT_TOOL, bad])
    assert [t["name"] for t in tools] == [DIRECT_ANSWER, "light_on"]
    assert tools[0]["eager_input_streaming"] is True
    assert "eager_input_streaming" not in tools[1]
    assert tools[1]["input_schema"] == LIGHT_TOOL["function"]["parameters"]
    assert tools[1]["description"] == "Turn a light on"
    assert provider._convert_tools([]) is None
    assert provider._last_tools == tools


def test_convert_tools_no_eager_streaming_with_base_url():
    provider = make_provider({**SONNET_CONFIG, "base_url": "https://proxy.example"})
    tools = provider._convert_tools([DIRECT_ANSWER_TOOL])
    assert "eager_input_streaming" not in tools[0]


# ------------------------------------------------------------ param policy


def test_params_sonnet_default():
    provider = make_provider({**SONNET_CONFIG, "max_tokens": 777})
    tools = provider._convert_tools([LIGHT_TOOL])
    p = provider._request_params("claude-sonnet-5-5", "sys", [{"role": "user", "content": []}], tools)
    assert p["thinking"] == {"type": "between_tools"}
    assert p["output_config"] == {"effort": "low"}
    assert p["tool_choice"] == {"type": "auto"}
    assert p["max_tokens"] == 777 and p["system"] == "sys"
    for key in ("temperature", "top_p", "top_k", "display", "budget_tokens", "block_binding"):
        assert key not in p and key not in p["thinking"]


def test_params_adaptive_omits_thinking_field():
    provider = make_provider({**SONNET_CONFIG, "thinking": "adaptive", "effort": "medium"})
    p = provider._request_params("claude-sonnet-5-5", None, [], None)
    assert "thinking" not in p and p["output_config"] == {"effort": "medium"} and "system" not in p


def test_params_between_tools_clamps_effort():
    provider = make_provider({**SONNET_CONFIG, "effort": "xhigh"})
    assert provider.effort == "high"


def test_params_haiku_and_other_models():
    provider = make_provider({**SONNET_CONFIG, "model_name": "claude-haiku-4-5-20251001"})
    p = provider._request_params(provider.model_name, None, [], None)
    assert "thinking" not in p and "output_config" not in p
    p = provider._request_params("claude-opus-5-5", None, [], None)
    assert "thinking" not in p and p["output_config"] == {"effort": "low"}


def test_response_kwargs_forward_max_tokens_drop_temperature():
    provider = make_provider(SONNET_CONFIG, FakeStream([ev_start(0, SimpleNamespace(type="text")), ev_text(0, "hi")], final_message()))
    out = list(provider.response("", [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], temperature=0.2, max_tokens=2000))
    params = provider.client.messages.calls[0]
    assert out == ["hi"] and params["max_tokens"] == 2000 and "temperature" not in params


def test_max_depth_path_sends_last_tools_with_tool_choice_none():
    provider = make_provider(SONNET_CONFIG, FakeStream([ev_text(0, "done")], final_message()))
    provider._convert_tools([LIGHT_TOOL])
    dialogue = [
        {"role": "user", "content": "lights"},
        {"role": "assistant", "tool_calls": [{"id": "toolu_1", "function": {"name": "light_on", "arguments": "{}"}, "type": "function", "index": 0}]},
        {"role": "tool", "tool_call_id": "toolu_1", "content": "ok"},
        {"role": "user", "content": "[系统提示] stop"},
    ]
    assert list(provider.response("", dialogue)) == ["done"]
    params = provider.client.messages.calls[0]
    assert params["tools"] == provider._last_tools and params["tool_choice"] == {"type": "none"}


def test_no_tool_blocks_sends_no_tools():
    provider = make_provider(SONNET_CONFIG, FakeStream([ev_text(0, "x")], final_message()))
    provider._convert_tools([LIGHT_TOOL])
    list(provider.response("", [{"role": "user", "content": "hi"}]))
    assert "tools" not in provider.client.messages.calls[0]


# ------------------------------------------------------------- streaming


def test_stream_thinking_and_text_before_tool_gives_ordinal_zero():
    events = [
        ev_start(0, SimpleNamespace(type="thinking")),
        ev_thinking(0, ""),
        ev_signature(0),
        ev_stop(0),
        ev_start(1, SimpleNamespace(type="text")),
        ev_text(1, "Sure,"),
        ev_stop(1),
        ev_start(2, tool_block("toolu_1", "light_on")),
        ev_json(2, ""),
        ev_json(2, '{"entity'),
        ev_json(2, '_id":"light.living"}'),
        ev_stop(2),
        SimpleNamespace(type="message_delta", delta=SimpleNamespace(stop_reason="tool_use")),
    ]
    provider = make_provider(SONNET_CONFIG, FakeStream(events, final_message("tool_use")))
    items = list(provider.response_with_functions("s", [{"role": "user", "content": "lights"}], functions=[LIGHT_TOOL]))
    assert items[0] == ("Sure,", None)
    assert len(items) == 2
    tool = items[1][1][0]
    assert tool.index == 0 and tool.id == "toolu_1" and tool.function.name == "light_on"
    calls = []
    for _, tools in items:
        if tools:
            merge_tool_calls(calls, tools)
    assert calls == [{"id": "toolu_1", "name": "light_on", "arguments": '{"entity_id":"light.living"}'}]
    assert json.loads(calls[0]["arguments"]) == {"entity_id": "light.living"}


def test_stream_direct_answer_streams_partial_deltas():
    events = [
        ev_start(0, tool_block("toolu_da", DIRECT_ANSWER)),
        ev_json(0, '{"response": "Hel'),
        ev_json(0, 'lo there"}'),
        ev_stop(0),
    ]
    provider = make_provider(SONNET_CONFIG, FakeStream(events, final_message("tool_use")))
    texts, calls, tool_yields = drive(provider, [{"role": "user", "content": "hi"}], [DIRECT_ANSWER_TOOL])
    assert texts == []
    assert tool_yields == 3  # start + one item per delta: TTS can begin before the block closes
    assert calls == [{"id": "toolu_da", "name": DIRECT_ANSWER, "arguments": '{"response": "Hello there"}'}]


def test_stream_parallel_tools_get_ordinals_zero_and_one():
    events = [
        ev_start(0, SimpleNamespace(type="text")),
        ev_text(0, "ok"),
        ev_start(1, tool_block("toolu_1", "light_on")),
        ev_json(1, '{"entity_id":"a"}'),
        ev_stop(1),
        ev_start(2, tool_block("toolu_2", "light_on")),
        ev_json(2, '{"entity_id":"b"}'),
        ev_stop(2),
    ]
    provider = make_provider(SONNET_CONFIG, FakeStream(events, final_message("tool_use")))
    _, calls, _ = drive(provider, [{"role": "user", "content": "both"}], [LIGHT_TOOL])
    assert [c["id"] for c in calls] == ["toolu_1", "toolu_2"]
    assert [json.loads(c["arguments"])["entity_id"] for c in calls] == ["a", "b"]


def test_stream_truncated_tool_dropped_with_warning_text():
    events = [
        ev_start(0, tool_block("toolu_1", "light_on")),
        ev_json(0, '{"entity_id":"li'),
        ev_stop(0),
    ]
    provider = make_provider(SONNET_CONFIG, FakeStream(events, final_message("max_tokens")))
    items = list(provider.response_with_functions("s", [{"role": "user", "content": "x"}], functions=[LIGHT_TOOL]))
    assert items == [(TRUNCATED_TEXT, None)]


def test_stream_truncated_without_stop_event_is_dropped():
    events = [ev_start(0, tool_block("toolu_1", "light_on")), ev_json(0, '{"entity_id":"li')]
    provider = make_provider(SONNET_CONFIG, FakeStream(events, final_message("max_tokens")))
    items = list(provider.response_with_functions("s", [{"role": "user", "content": "x"}], functions=[LIGHT_TOOL]))
    assert items == [(TRUNCATED_TEXT, None)]


def test_stream_refusal_yields_fallback_text_only_when_silent():
    provider = make_provider(SONNET_CONFIG, FakeStream([], final_message("refusal", "general_harms")))
    assert list(provider.response("", [{"role": "user", "content": "x"}])) == [REFUSAL_TEXT]
    provider = make_provider(SONNET_CONFIG, FakeStream([ev_text(0, "no")], final_message("refusal", "cyber")))
    assert list(provider.response("", [{"role": "user", "content": "x"}])) == ["no"]


def test_response_no_stream_joins_text_and_ignores_tools():
    events = [
        ev_text(0, "Hel"),
        ev_text(0, "lo"),
        ev_start(1, tool_block("toolu_1", "light_on")),
        ev_json(1, "{}"),
        ev_stop(1),
    ]
    provider = make_provider(SONNET_CONFIG, FakeStream(events, final_message()))
    assert provider.response_no_stream("sys", "hi") == "Hello"
    params = provider.client.messages.calls[0]
    assert params["system"] == "sys" and params["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def test_stream_closes_on_generator_exit():
    stream = FakeStream([ev_text(0, "a"), ev_text(0, "b")], final_message())
    provider = make_provider(SONNET_CONFIG, stream)
    gen = provider.response("", [{"role": "user", "content": "x"}])
    assert next(gen) == "a"
    gen.close()
    assert stream.closed


# --------------------------------------------------------------- fallback


@pytest.mark.parametrize(
    "error",
    [
        api_error(anthropic.RateLimitError, 429),
        api_error(anthropic.InternalServerError, 500),
        api_error(anthropic.OverloadedError, 529),
        api_error(anthropic.APIStatusError, 200),
    ],
)
def test_fallback_switches_model_before_first_yield(error):
    config = {**SONNET_CONFIG, "fallback_model_name": "claude-haiku-4-5-20251001"}
    provider = make_provider(config, error, FakeStream([ev_text(0, "fallback")], final_message()))
    items = list(provider.response_with_functions("s", [{"role": "user", "content": "x"}], functions=[LIGHT_TOOL]))
    assert items == [("fallback", None)]
    first, second = provider.client.messages.calls
    assert first["model"] == "claude-sonnet-5-5" and "thinking" in first
    assert second["model"] == "claude-haiku-4-5-20251001"
    assert "thinking" not in second and "output_config" not in second
    assert second["tools"] == first["tools"]


def test_no_fallback_after_first_yield():
    class ErroringStream(FakeStream):
        def __iter__(self):
            yield ev_text(0, "partial")
            raise api_error(anthropic.OverloadedError, 529)

    config = {**SONNET_CONFIG, "fallback_model_name": "claude-haiku-4-5-20251001"}
    provider = make_provider(config, ErroringStream([], final_message()), FakeStream([ev_text(0, "x")], final_message()))
    gen = provider.response("", [{"role": "user", "content": "x"}])
    assert next(gen) == "partial"
    with pytest.raises(anthropic.OverloadedError):
        next(gen)
    assert len(provider.client.messages.calls) == 1


def test_bad_request_is_reraised_without_fallback():
    config = {**SONNET_CONFIG, "fallback_model_name": "claude-haiku-4-5-20251001"}
    provider = make_provider(config, api_error(anthropic.BadRequestError, 400))
    with pytest.raises(anthropic.BadRequestError):
        list(provider.response("", [{"role": "user", "content": "x"}]))
    assert len(provider.client.messages.calls) == 1


def test_no_fallback_configured_reraises():
    provider = make_provider(SONNET_CONFIG, api_error(anthropic.RateLimitError, 429))
    with pytest.raises(anthropic.RateLimitError):
        list(provider.response("", [{"role": "user", "content": "x"}]))


# ----------------------------------------------------------------- config


def test_init_defaults_and_client_settings():
    provider = LLMProvider({"api_key": "sk-test"})
    assert provider.model_name == "claude-sonnet-5-5"
    assert provider.fallback_model_name is None
    assert provider.effort == "low" and provider.thinking == "adaptive"
    assert "thinking" not in provider._request_params("claude-sonnet-5-5", None, [], None)
    assert provider.max_tokens == 1024
    assert provider.client.max_retries == 1
    assert provider.client.timeout == 60.0


def test_init_timeout_dict_and_missing_key():
    provider = LLMProvider({"timeout": {"read": 30}, "max_retries": 0})
    assert provider.api_key is None
    assert provider.client.max_retries == 0
    assert provider.client.timeout.read == 30
