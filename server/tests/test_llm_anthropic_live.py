"""Live smoke test for the Anthropic provider.

Runs only with RUN_LIVE_TESTS=1 and ANTHROPIC_API_KEY set; makes a handful of
short real requests and prints first-token latency per thinking mode.
"""
import json
import os
import re
import time
from pathlib import Path

import pytest
from jinja2 import Template

from core.providers.llm.anthropic.anthropic import DIRECT_ANSWER, LLMProvider

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1" or not os.environ.get("ANTHROPIC_API_KEY"),
    reason="set RUN_LIVE_TESTS=1 and ANTHROPIC_API_KEY",
)

PROMPT_FILE = Path(__file__).resolve().parents[1] / "overlay" / "weixue-agent-prompt.txt"
IDENTITY = "You are Jarvis, the voice assistant of this household. Keep replies short."
CJK = re.compile(r"[一-鿿]")

DIRECT_ANSWER_TOOL = {
    "type": "function",
    "function": {
        "name": DIRECT_ANSWER,
        "description": "当用户的请求不匹配其他任何工具时，可用此选项直接回复。将回复内容写在response参数里。",
        "parameters": {
            "type": "object",
            "properties": {"response": {"type": "string", "description": "你回复用户的完整内容"}},
            "required": ["response"],
        },
    },
}
LIGHT_TOOL = {
    "type": "function",
    "function": {
        "name": "HassTurnOn",
        "description": "Turn on a Home Assistant entity (light, switch, fan). Living room light is light.living_room.",
        "parameters": {
            "type": "object",
            "properties": {"entity_id": {"type": "string", "description": "Entity id such as light.living_room"}},
            "required": ["entity_id"],
        },
    },
}
TOOLS = [LIGHT_TOOL, DIRECT_ANSWER_TOOL]


def system_prompt():
    template = Template(PROMPT_FILE.read_text(encoding="utf-8"))
    rendered = template.render(
        base_prompt=IDENTITY,
        current_time="{{current_time}}",
        today_date="2026-10-06",
        today_weekday="Tuesday",
        lunar_date="",
        local_address="",
        weather_info="",
        emojiList="",
        device_id="test",
        client_ip="",
        dynamic_context="",
        language="中文",
        emoji_enabled=False,
    )
    return rendered.replace("{{current_time}}", "19:30")


def provider_for(thinking):
    return LLMProvider(
        {
            "api_key": os.environ["ANTHROPIC_API_KEY"],
            "model_name": os.environ.get("WEIXUE_LLM_MODEL", "claude-sonnet-5-5"),
            "fallback_model_name": "claude-haiku-4-5-20251001",
            "thinking": thinking,
            "effort": "low",
            "max_tokens": 512,
            "timeout": 60,
            "max_retries": 1,
        }
    )


def run(provider, user_text, dialogue_prefix=()):
    """Drive one request; returns (first_token_s, texts, tool_calls, total_s)."""
    dialogue = [{"role": "system", "content": system_prompt()}, *dialogue_prefix, {"role": "user", "content": user_text}]
    texts, calls = [], {}
    first = None
    start = time.monotonic()
    for text, tools in provider.response_with_functions("live", dialogue, functions=TOOLS):
        if first is None and (text or tools):
            first = time.monotonic() - start
        if text:
            texts.append(text)
        for t in tools or []:
            entry = calls.setdefault(t.index, {"id": "", "name": "", "arguments": ""})
            if t.id:
                entry["id"] = t.id
            if t.function.name:
                entry["name"] = t.function.name
            if t.function.arguments:
                entry["arguments"] += t.function.arguments
    return first, texts, [calls[k] for k in sorted(calls)], time.monotonic() - start


@pytest.mark.parametrize("thinking", ["between_tools", "adaptive"])
def test_direct_answer_english_and_latency(thinking):
    first, texts, calls, total = run(provider_for(thinking), "Tell me a very short joke.")
    print(f"\n[{thinking}] first_token={first:.2f}s total={total:.2f}s texts={texts} calls={calls}")
    reply = "".join(texts) or json.loads(calls[0]["arguments"])["response"]
    assert reply.strip()
    assert not CJK.search(reply)
    assert all(c["name"] == DIRECT_ANSWER for c in calls)


def test_direct_answer_chinese():
    first, texts, calls, total = run(provider_for("between_tools"), "讲个很短的笑话。")
    print(f"\n[zh] first_token={first:.2f}s total={total:.2f}s texts={texts} calls={calls}")
    reply = "".join(texts) or json.loads(calls[0]["arguments"])["response"]
    assert CJK.search(reply)
    assert all(c["name"] == DIRECT_ANSWER for c in calls)


def test_tool_round_trip():
    provider = provider_for("between_tools")
    first, texts, calls, total = run(provider, "Turn on the living room light.")
    print(f"\n[tool] first_token={first:.2f}s total={total:.2f}s texts={texts} calls={calls}")
    assert len(calls) == 1 and calls[0]["name"] == "HassTurnOn"
    args = json.loads(calls[0]["arguments"])
    assert args["entity_id"].startswith("light.")
    assert "".join(texts).strip() == "", "no narration before the tool call"

    prefix = [
        {"role": "user", "content": "Turn on the living room light."},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": calls[0]["id"], "function": {"name": "HassTurnOn", "arguments": calls[0]["arguments"]}, "type": "function", "index": 0}
            ],
        },
        {"role": "tool", "tool_call_id": calls[0]["id"], "content": json.dumps({"entity_id": args["entity_id"], "state": "on"})},
    ]
    # second request: history carries tool_use/tool_result and no thinking blocks
    dialogue = [{"role": "system", "content": system_prompt()}, *prefix]
    start = time.monotonic()
    items = list(provider.response_with_functions("live", dialogue, functions=[LIGHT_TOOL]))
    texts2 = "".join(t for t, _ in items if t)
    tools2 = [t for _, tl in items if tl for t in tl]
    print(f"[tool/2] total={time.monotonic() - start:.2f}s text={texts2!r} tools={len(tools2)}")
    assert texts2.strip() and not tools2
    assert not CJK.search(texts2)


def test_haiku_model_params_accepted():
    provider = provider_for("between_tools")
    provider.model_name = "claude-haiku-4-5-20251001"
    first, texts, calls, total = run(provider, "Say hello in three words.")
    print(f"\n[haiku] first_token={first:.2f}s total={total:.2f}s texts={texts} calls={calls}")
    assert ("".join(texts).strip() or calls)
