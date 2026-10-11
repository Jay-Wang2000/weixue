"""JSON-lines logging to stdout with secret redaction."""
import json
import re
import sys
import threading
from typing import Any, Iterable

from . import timeutil

_SECRETS: set = set()
_LOCK = threading.Lock()
_SENSITIVE_KEY = re.compile(r"(token|pin|password|secret|key|url)", re.I)
_PATTERNS = [
    # Ends at the END line, an unescaped quote or the end of the string; an escape pair is consumed whole.
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----(?:\\.|[^"\\])*?'
               r'(?:-----END [A-Z ]*PRIVATE KEY-----|(?=")|(?=\\?\Z))', re.S),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+"),
    re.compile(r"https?://[^\s\"'<>]*private_[^\s\"'<>]*"),
    re.compile(r"private_[A-Za-z0-9_\-]+"),
    re.compile(r"token=[^\s&\"'<>]+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),
]
ARG_STR_MAX = 120
_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}
_min_level = {"value": 20}


def set_level(name: str) -> None:
    _min_level["value"] = _LEVELS.get(name.lower(), 20)


def register_secret(value: str | None) -> None:
    """Exact strings that are replaced by [redacted] wherever they appear."""
    if value and len(value) >= 8:
        with _LOCK:
            _SECRETS.add(value)


def redact_text(text: str) -> str:
    if not text:
        return text
    for secret in sorted(_SECRETS, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, "[redacted]")
    for pattern in _PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def scrub(value: Any, depth: int = 0) -> Any:
    """Every string inside `value` redacted; structure and lengths otherwise unchanged."""
    if depth > 20:
        return redact_text(json.dumps(value, ensure_ascii=False, default=str))
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: scrub(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, depth + 1) for v in value]
    return value


def redact_args(value: Any, depth: int = 0) -> Any:
    """Tool arguments for logs and the trace: sensitive keys hidden, strings capped."""
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        return {
            str(k): "[redacted]" if _SENSITIVE_KEY.search(str(k)) else redact_args(v, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_args(v, depth + 1) for v in list(value)[:50]]
    if isinstance(value, str):
        text = redact_text(value)
        return text if len(text) <= ARG_STR_MAX else text[: ARG_STR_MAX - 1] + "…"
    return value


def truncate(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def log(msg: str, level: str = "info", **fields: Any) -> None:
    if _LEVELS.get(level, 20) < _min_level["value"]:
        return
    record = {"ts": timeutil.iso(), "level": level, "msg": msg}
    for key, value in fields.items():
        if value is not None:
            record[key] = value
    line = json.dumps(scrub(json.loads(json.dumps(record, default=str))), ensure_ascii=False)
    with _LOCK:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def stderr_sink(prefix: str = "cli") -> Any:
    def sink(line: str) -> None:
        line = line.rstrip()
        if line:
            log("cli stderr", level="debug", part=prefix, detail=truncate(line, 300))
    return sink


def register_secrets(values: Iterable[str | None]) -> None:
    for value in values:
        register_secret(value)
