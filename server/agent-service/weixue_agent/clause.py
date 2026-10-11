"""The gateway TTS's first_clause rule (docs/server.md, Google Cloud TTS): where the first segment of a reply ends.
Kept identical to `first_clause_cut` in server/overlay/core/providers/tts/google_tts.py
(server/tests/test_agent_text_coalescing.py compares the two)."""
import re

FIRST_CLAUSE_WORDS = 2
FIRST_CLAUSE_HAN = 4
FIRST_CUT_WORDS = 8
FIRST_CUT_HAN = 12
_CLAUSE_MARKS = frozenset("，,、：:；;。.!?！？…—")
_MARK_RUN = frozenset(".!?…。！？")
_CLOSERS = frozenset("\"'”’)]）」』")
_DIGIT_MARKS = frozenset(".,:")
_HAN = re.compile(r"[\u3400-\u9fff]")
_WORD = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)*")
_WORD_THEN_SPACE = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)*(?=\s)")
_LAST_WORD = re.compile(r"(\S+)$")
_ABBREVIATIONS = frozenset(
    "mr mrs ms dr prof sr jr st mt ft vs etc approx e.g i.e a.m p.m u.s u.k inc ltd "
    "jan feb mar apr jun jul aug sep sept oct nov dec fig".split()
)


def _abbreviation_before(text: str, i: int) -> bool:
    word = _LAST_WORD.search(text[:i])
    token = word.group(1).lstrip("([\"'“‘").lower() if word else ""
    return token in _ABBREVIATIONS or (len(token) == 1 and token.isalpha())


def _holds(prefix: str, words: int, han: int) -> bool:
    return len(_WORD.findall(prefix)) >= words or len(_HAN.findall(prefix)) >= han


def _word_cut(text: str, limit: int) -> int:
    space = max(text.rfind(" ", 0, limit), text.rfind("\n", 0, limit))
    return space + 1 if space > 0 else limit


def first_clause_cut(text: str) -> int:
    """Index just past the first segment of a reply in text, 0 while it is not complete."""
    for i, ch in enumerate(text):
        if ch not in _CLAUSE_MARKS:
            continue
        prev = text[i - 1] if i else ""
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if ch in _DIGIT_MARKS and prev.isdigit() and (nxt == "" or nxt.isdigit()):
            continue
        if ch == "." and ((nxt and not nxt.isspace() and nxt not in _CLOSERS and nxt != ".")
                          or _abbreviation_before(text, i)):
            continue
        end = i + 1
        while end < len(text) and (text[end] in _MARK_RUN or text[end] in _CLOSERS):
            end += 1
        if _holds(text[:end], FIRST_CLAUSE_WORDS, FIRST_CLAUSE_HAN):
            return end
    if len(_HAN.findall(text)) >= FIRST_CUT_HAN:
        return len(text)
    if len(_WORD_THEN_SPACE.findall(text)) >= FIRST_CUT_WORDS:
        return _word_cut(text, len(text))
    return 0
