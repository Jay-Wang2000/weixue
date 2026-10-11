"""Activity label of a working task: what the agent is doing now, from its current tool (decision 68)."""
from typing import Callable, Optional

from . import app_tools

SEARCH = "search"
HOUSE = "house"
ACT = "act"
FIND_TOOL = "find_tool"
THINK = "think"

LABELS = {
    SEARCH: {"en": "Searching…", "zh": "搜索中…"},
    HOUSE: {"en": "Checking the house…", "zh": "查看家里状态…"},
    ACT: {"en": "Acting…", "zh": "正在操作…"},
    FIND_TOOL: {"en": "Finding a tool…", "zh": "找工具…"},
    THINK: {"en": "Thinking…", "zh": "思考中…"},
}
MAX_CHARS = 24

# Code execution is not a request tool here: it runs only inside the web tools' dynamic filtering.
SEARCH_TOOLS = ("web_search", "web_fetch", "code_execution", "bash_code_execution", "text_editor_code_execution")
HA_READ_PREFIXES = ("ha_get_", "ha_list_", "ha_search")


def kind_of(tool: Optional[str], server: bool, ha_read_only: Callable[[str], bool]) -> str:
    if not tool:
        return THINK
    if tool in SEARCH_TOOLS:
        return SEARCH
    if tool.startswith("tool_search"):
        return FIND_TOOL
    if server or tool == "memory" or tool in app_tools.NAMES:
        return THINK
    return HOUSE if tool.startswith(HA_READ_PREFIXES) or ha_read_only(tool) else ACT


def label(kind: str, lang: str) -> str:
    return LABELS[kind]["zh" if lang == "zh" else "en"]
