"""Model of ha-mcp 8.6.0 `require_approval` evaluation, used only by the e2e guard (AGENT_E2E_GUARD=1).

Production never consults it: the live ha-mcp policy alone decides held versus free.
"""
import re
from typing import Any, Dict, Iterable, List


def _values(args: Dict[str, Any], path: str) -> List[Any]:
    parts = path.split(".")
    if parts[0] == "args":
        parts = parts[1:]

    def walk(cur: Any, rest: List[str]) -> Iterable[Any]:
        if not rest:
            yield cur
            return
        head, tail = rest[0], rest[1:]
        if head == "*":
            children = list(cur.values()) if isinstance(cur, dict) else list(cur) if isinstance(cur, (list, tuple)) else []
        elif isinstance(cur, dict) and head in cur:
            children = [cur[head]]
        else:
            children = []
        for child in children:
            yield from walk(child, tail)

    return list(walk(args, parts))


def _ci(x: Any) -> Any:
    return x.lower() if isinstance(x, str) else x


def _op(value: Any, op: str, expected: Any) -> bool:
    if op == "eq":
        return _ci(value) == _ci(expected)
    if op == "neq":
        return _ci(value) != _ci(expected)
    if op == "in":
        return _ci(value) in [_ci(x) for x in expected]
    if op == "not_in":
        return _ci(value) not in [_ci(x) for x in expected]
    if op == "regex":
        return isinstance(value, str) and re.search(expected, value, re.IGNORECASE) is not None
    if op in ("gt", "lt"):
        try:
            return value > expected if op == "gt" else value < expected
        except TypeError:
            return False
    return True


def _predicate(predicate: Dict[str, Any], args: Dict[str, Any]) -> bool:
    values = _values(args, predicate["path"])
    if not values:
        return False
    if predicate["op"] == "exists":
        return True
    return any(_op(v, predicate["op"], predicate.get("value")) for v in values)


def held(rules: List[Dict[str, Any]], tool: str, args: Dict[str, Any]) -> bool:
    for rule in rules:
        if rule.get("tool_name") in ("*", tool) and all(_predicate(p, args) for p in rule.get("when") or []):
            return True
    return tool == "ha_call_service" and bool(args.get("ws_command"))
