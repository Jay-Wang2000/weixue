"""Voice-path tool allowlist and ha_call_service domain allowlist."""
from typing import Any, List, Optional

from .config import VoiceFilter

CALL_SERVICE = "ha_call_service"


def filter_tools(tools: List[Any], voice_filter: VoiceFilter) -> List[Any]:
    return [t for t in tools if isinstance(t, dict) and t.get("name") in voice_filter.tools]


def refusal(voice_filter: VoiceFilter, name: Any, args: Any) -> Optional[str]:
    """Why the voice path refuses this call, or None to forward it."""
    if name not in voice_filter.tools:
        return f"Not available by voice: tool {name!r} is not enabled on the voice path."
    if name != CALL_SERVICE:
        return None
    if not isinstance(args, dict):
        return "Not available by voice: ha_call_service arguments must be an object."
    if args.get("ws_command") not in (None, ""):
        return "Not available by voice: raw WebSocket commands are disabled on the voice path."
    domain = args.get("domain")
    if not isinstance(domain, str) or domain.strip().lower() not in voice_filter.call_service_domains:
        return (
            f"Not available by voice: {domain!r} services are disabled on the voice path. "
            "Tell the user this has to be done in the Home Assistant app."
        )
    return None
