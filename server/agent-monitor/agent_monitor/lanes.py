"""Connection lanes from health_probes rows: ok or bad per row, unknown where rows stop arriving."""
from typing import Any, Dict, List

from . import queries

# agent-service writes a probe row when ok flips and at least every 300 s.
PROBE_ROW_GAP_S = 660
LABELS = {"ha_mcp": "ha-mcp", "mcp_endpoint": "Endpoint", "model_api": "Model API", "tunnel": "Tunnel"}


def _epoch(ts: str) -> float:
    return queries._parse(ts).timestamp()


def segments(rows: List[Dict[str, Any]], start: float, end: float, gap_s: float = PROBE_ROW_GAP_S) -> List[Dict[str, Any]]:
    """Contiguous [from, to) segments covering start..end, merged when the state repeats."""
    raw = []
    points = [(_epoch(r["ts"]), r["ok"]) for r in rows]
    for i, (t, ok) in enumerate(points):
        nxt = points[i + 1][0] if i + 1 < len(points) else end
        stop = min(nxt, t + gap_s)
        raw.append((t, stop, "ok" if ok else "bad"))
        if stop < nxt:
            raw.append((stop, nxt, "unknown"))
    out: List[List[Any]] = []

    def add(a: float, b: float, state: str) -> None:
        if out and out[-1][2] == state and out[-1][1] >= a:
            out[-1][1] = b
        else:
            out.append([a, b, state])

    cursor = start
    for a, b, state in raw:
        a, b = max(a, start), min(b, end)
        if b <= a:
            continue
        if a > cursor:
            add(cursor, a, "unknown")
        add(a, b, state)
        cursor = b
    if cursor < end:
        add(cursor, end, "unknown")
    return [{"from": a, "to": b, "state": state} for a, b, state in out]


def lanes(history: Dict[str, List[Dict[str, Any]]], start: float, end: float) -> List[Dict[str, Any]]:
    out = []
    for probe in sorted(history, key=lambda p: list(LABELS).index(p) if p in LABELS else len(LABELS)):
        rows = history[probe]
        last = rows[-1] if rows else None
        out.append({"probe": probe, "label": LABELS.get(probe, probe), "segments": segments(rows, start, end),
                    "latency_ms": last["latency_ms"] if last else None,
                    "flips": sum(1 for a, b in zip(rows, rows[1:]) if a["ok"] != b["ok"])})
    return out
