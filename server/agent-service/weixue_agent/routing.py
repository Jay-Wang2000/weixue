"""Lexicons and text classification for routing a new user message (brief section 7)."""
import re
from typing import Optional

CJK = re.compile(r"[㐀-鿿豈-﫿]")
_STRIP = re.compile(r"[\s\W_]+", re.UNICODE)

STOP = {"stop", "cancel", "never mind", "nevermind", "停", "停下", "取消", "算了"}
APPROVE = {"approve", "do it", "批准", "同意"}
DENY = {"no", "deny", "cancel", "stop", "不", "不要", "拒绝", "取消"}
BARE_YES = {"yes", "ok", "okay", "好", "是", "好的", "是的"}
QUESTION_ENDS = ("?", "？", "吗", "吗。", "吗？")

STOPPED = {"en": "Stopped.", "zh": "好，停了。"}
MAYBE_HAPPENED = {"en": " Some of it may already have happened.", "zh": "有些可能已经执行了。"}
BUSY = {"en": "I'm busy right now, try again in a moment.", "zh": "我这会儿忙，稍后再试。"}
ACK = {"en": "On it.", "zh": "好的，我来处理。"}
STEER_ACK = {"en": "One moment.", "zh": "稍等。"}
OKAY = {"en": "Okay.", "zh": "好的。"}
DENIED = {"en": "Okay, I won't do that.", "zh": "好，那就不做了。"}


def detect_lang(text: str, requested: str = "auto") -> str:
    if requested in ("en", "zh"):
        return requested
    return "zh" if CJK.search(text) else "en"


def normalize(text: str) -> str:
    """Lowercase, punctuation and surrounding space removed, inner whitespace collapsed to one space."""
    words = [w for w in _STRIP.split(text.lower()) if w]
    return " ".join(words)


def is_stop(text: str) -> bool:
    return normalize(text) in STOP


def voice_decision(text: str) -> Optional[str]:
    """'approve', 'deny', 'yes' (needs a repeat-back) or None for any other text (exact phrases only)."""
    norm = normalize(text)
    if norm in APPROVE:
        return "approve"
    if norm in DENY:
        return "deny"
    if norm in BARE_YES:
        return "yes"
    return None


def ends_with_question(text: str) -> bool:
    return text.strip().endswith(QUESTION_ENDS)


def approval_cue(lang: str, action_text: str) -> str:
    if lang == "zh":
        return f"需要你在面板上确认：{action_text}。"
    return f"I need your OK on the panel to {action_text}."


def repeat_back(lang: str, action_text: str) -> str:
    if lang == "zh":
        return f"要执行「{action_text}」，请说「批准」，或在面板上点确认。"
    return f"Say approve to {action_text}, or tap the panel."


def describe_for_speech(action_text: str) -> str:
    """'light.turn_on light.desk' → 'turn on light.desk' for spoken cues."""
    head, _, rest = action_text.partition(" ")
    if "." in head:
        service = head.split(".", 1)[1].replace("_", " ")
        return f"{service} {rest}".strip()
    return action_text
