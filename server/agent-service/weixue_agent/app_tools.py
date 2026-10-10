"""App tools of a chat: run in-process by the turn loop, applied to the chat's turn in flight."""
from typing import Any, Awaitable, Callable, Dict, List

from anthropic.lib.tools import BetaAsyncFunctionTool, beta_async_tool

NAMES = ("ask_user", "start_task", "set_chat_title", "notify_user", "set_result_tier", "awaiting_answer")

_SPECS: Dict[str, Dict[str, Any]] = {
    "ask_user": {
        "description": ("Ask the user a question you need answered before you can continue. The question is "
                        "spoken and shown on the panel with up to four short options. The result is their answer, "
                        "or a note that they have not answered yet."),
        "input_schema": {
            "type": "object", "additionalProperties": False, "required": ["question"],
            "properties": {
                "question": {"type": "string", "description": "One short spoken question."},
                "options": {"type": "array", "maxItems": 4, "items": {"type": "string"},
                            "description": "Up to four short answer options (a few words each)."},
                "multi_select": {"type": "boolean", "description": "True when several options may be chosen."},
            },
        },
    },
    "start_task": {
        "description": ("Start longer work: call this after your short spoken acknowledgement when the request "
                        "needs several tool calls, research or more than a few seconds. You then continue the "
                        "work in this turn; the result is reported to the user when you finish."),
        "input_schema": {
            "type": "object", "additionalProperties": False, "required": ["title"],
            "properties": {"title": {"type": "string", "description": "A short title for the work (a few words)."}},
        },
    },
    "set_chat_title": {
        "description": "Give this chat a short title (at most 32 characters) once its topic is clear.",
        "input_schema": {
            "type": "object", "additionalProperties": False, "required": ["title"],
            "properties": {"title": {"type": "string"}},
        },
    },
    "notify_user": {
        "description": ("Speak a short message to the user now. urgent=true interrupts whatever the device is doing "
                        "(safety or time-critical only); otherwise it waits until the device is idle."),
        "input_schema": {
            "type": "object", "additionalProperties": False, "required": ["text"],
            "properties": {"text": {"type": "string"}, "urgent": {"type": "boolean"}},
        },
    },
    "set_result_tier": {
        "description": ("How this task's result is delivered when it finishes after the conversation ended: "
                        "'interrupt' speaks it at once, 'silent' only shows a card. Default: spoken when the device "
                        "is idle."),
        "input_schema": {
            "type": "object", "additionalProperties": False, "required": ["tier"],
            "properties": {"tier": {"enum": ["interrupt", "silent", "when_idle"]}},
        },
    },
    "awaiting_answer": {
        "description": "Call this when your final reply asks the user a question and you need the answer.",
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {}},
    },
}


def build(handle: Callable[[str, Dict[str, Any]], Awaitable[str]]) -> List[BetaAsyncFunctionTool]:
    """`handle(name, input)` applies one call to the chat's turn in flight and returns the result text."""
    tools = []
    for name in NAMES:
        spec = _SPECS[name]
        tools.append(beta_async_tool(_bind(handle, name), name=name, description=spec["description"],
                                     input_schema=spec["input_schema"]))
    return tools


def _bind(handle: Callable[[str, Dict[str, Any]], Awaitable[str]], name: str):
    async def call(**kwargs: Any) -> str:
        return await handle(name, kwargs)

    call.__name__ = name
    return call
