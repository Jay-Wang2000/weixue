"""agent-service's first-clause rule (agent-service/weixue_agent/clause.py, used by its TextCoalescer) equals the
gateway TTS's first_clause_cut on every prefix of a streamed reply."""
import importlib.util

import pytest

from core.providers.tts import google_tts
from tests.conftest import SERVER_DIR

SAMPLES = [
    "On it.",
    "Let me check. The kitchen light is on. Ok",
    "Sure, it is ten past nine. And",
    "Yes, the light is on, and",
    "Okay — done",
    "Okay, I'll — done",
    "Dr. Smith is in. Ok",
    "It is 3.5 degrees now, sir",
    "It is 3. Then",
    "Meet at 9 a.m. tomorrow",
    'He said "stop." Then',
    "Wait... ok",
    "one two three four five six seven eight nine",
    "好的，我来处理。然后",
    "我来帮你看看，客厅的灯",
    "今天天气很好我们一起去公园散步",
    "The 客厅灯 is on, 好的",
    "Sure, switching to bedside mode.",
    "At 10:30, the timer",
]


def _service_clause():
    path = SERVER_DIR / "agent-service" / "weixue_agent" / "clause.py"
    spec = importlib.util.spec_from_file_location("agent_service_clause", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("text", SAMPLES)
def test_service_first_clause_matches_the_tts_rule_on_every_prefix(text):
    service = _service_clause()
    for end in range(len(text) + 1):
        assert service.first_clause_cut(text[:end]) == google_tts.first_clause_cut(text[:end]), text[:end]


def test_service_first_clause_constants_match_the_tts():
    service = _service_clause()
    for name in ("FIRST_CLAUSE_WORDS", "FIRST_CLAUSE_HAN", "FIRST_CUT_WORDS", "FIRST_CUT_HAN", "_CLAUSE_MARKS",
                 "_MARK_RUN", "_CLOSERS", "_DIGIT_MARKS", "_ABBREVIATIONS"):
        assert getattr(service, name) == getattr(google_tts, name), name
    for name in ("_HAN", "_WORD", "_WORD_THEN_SPACE", "_LAST_WORD"):
        assert getattr(service, name).pattern == getattr(google_tts, name).pattern, name
