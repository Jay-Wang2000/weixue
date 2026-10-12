"""Severity of an `errors` row by (source, code): a failure needs a look, a notice is expected noise."""
from typing import Dict, List

NOTICES = {
    ("model", "transcript"),
    ("model", "model_fallback"),
    ("model", "retention"),
}
FOLD_GAP_S = 900


def severity(source: str, code: str) -> str:
    return "notice" if (source, code) in NOTICES else "failure"


def severity_map() -> List[Dict[str, str]]:
    return [{"source": s, "code": c, "severity": "notice"} for s, c in sorted(NOTICES)]
