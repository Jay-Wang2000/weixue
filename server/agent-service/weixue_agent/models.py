"""Request parameters of the turn loop: models per mode, betas, thinking, effort, server tools, prices."""
from typing import Any, Dict, List, Optional

QUICK = "quick"
TASK = "task"
MODELS = {QUICK: "claude-sonnet-5-5", TASK: "claude-fable-5-1"}
RETENTION_MODEL = "claude-opus-5-5"
EFFORT = {QUICK: "low", TASK: "high"}
TOP_LEVEL_EFFORT = "low"
MAX_TOKENS = 32000

BETAS = [
    "server-side-fallback-2026-07-01",
    "mid-conversation-output-config-2026-07-01",
    "thinking-display-updates-2026-08-18",
    "thinking-binding-controls-2026-08-01",
    "compact-2026-09-04",
]

HOT_HA_TOOLS = (
    "ha_search", "ha_get_state", "ha_get_history", "ha_list_floors_areas", "ha_call_service", "ha_get_todo",
    "ha_config_get_calendar_events",
)

TOOL_SEARCH = {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}
WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}
WEB_FETCH = {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5}
SERVER_TOOL_TYPES = {"server_tool_use", "web_search_tool_result", "web_fetch_tool_result",
                     "tool_search_tool_result", "bash_code_execution_tool_result",
                     "code_execution_tool_result", "text_editor_code_execution_tool_result"}

# USD per million tokens: input, output, cache read, 5-minute cache write.
PRICES = {
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-8": (5.0, 25.0, 0.50, 6.25),
}
WEB_SEARCH_USD = 0.01


def thinking(prefix_mismatch_behavior: str = "drop_block") -> Dict[str, Any]:
    return {"type": "adaptive", "display": "updates",
            "block_binding": {"prefix_mismatch_behavior": prefix_mismatch_behavior}}


def effort_message(effort: str) -> Dict[str, Any]:
    """Effort-only system message: changes effort from the next user turn without touching the cache."""
    return {"role": "system", "content": [], "output_config": {"effort": effort}}


def server_tools(model: str) -> List[Dict[str, Any]]:
    """Server-side tools of a request, a pure function of the model."""
    return [dict(TOOL_SEARCH), dict(WEB_SEARCH), dict(WEB_FETCH)]


def model_for(mode: str, task_model: Optional[str] = None) -> str:
    if mode == TASK:
        return task_model or MODELS[TASK]
    return MODELS[QUICK]


def request_kwargs(model: str, system: str, prefix_mismatch_behavior: str = "drop_block") -> Dict[str, Any]:
    """Everything of a turn's requests except `messages` and `tools`."""
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": system}],
        "thinking": thinking(prefix_mismatch_behavior),
        "output_config": {"effort": TOP_LEVEL_EFFORT},
        "fallbacks": "default",
        "cache_control": {"type": "ephemeral"},
        "betas": list(BETAS),
    }


def usage_numbers(usage: Any) -> Dict[str, int]:
    def get(name: str) -> int:
        return int(getattr(usage, name, 0) or 0)

    server = getattr(usage, "server_tool_use", None)
    return {
        "input_tokens": get("input_tokens"),
        "output_tokens": get("output_tokens"),
        "cache_read_tokens": get("cache_read_input_tokens"),
        "cache_creation_tokens": get("cache_creation_input_tokens"),
        "web_search_requests": int(getattr(server, "web_search_requests", 0) or 0) if server else 0,
        "web_fetch_requests": int(getattr(server, "web_fetch_requests", 0) or 0) if server else 0,
    }


def cost_usd(model: str, numbers: Dict[str, int]) -> float:
    price = PRICES.get(model) or PRICES[MODELS[TASK]]
    tokens = (numbers["input_tokens"] * price[0] + numbers["output_tokens"] * price[1]
              + numbers["cache_read_tokens"] * price[2] + numbers["cache_creation_tokens"] * price[3])
    return tokens / 1_000_000 + numbers["web_search_requests"] * WEB_SEARCH_USD


def total_input(numbers: Dict[str, int]) -> int:
    return numbers["input_tokens"] + numbers["cache_read_tokens"] + numbers["cache_creation_tokens"]
