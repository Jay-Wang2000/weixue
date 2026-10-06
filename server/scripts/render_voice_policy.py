#!/usr/bin/env python3
"""Print an ha-mcp policy document: BASE with the overlay in server/policy/voice_policy.proposed.json applied.

Usage: render_voice_policy.py BASE_POLICY.json [OVERLAY.json] > merged.json
Each `remove_rules` entry must match a base rule exactly; `add_rules` take the
position of the first removed rule. Exits non-zero on a missing rule or an
invalid result (wait_seconds outside 5-10 or not below approval_ttl_minutes*60).
"""
import json
import sys
from pathlib import Path

DEFAULT_OVERLAY = Path(__file__).resolve().parents[1] / "policy" / "voice_policy.proposed.json"
SETTINGS = {"wait_seconds", "approval_ttl_minutes"}


def apply_overlay(base: dict, overlay: dict) -> dict:
    if set(overlay) != {"settings", "remove_rules", "add_rules"}:
        raise ValueError("overlay keys must be exactly settings, remove_rules, add_rules")
    if not set(overlay["settings"]) <= SETTINGS:
        raise ValueError(f"overlay settings must be a subset of {sorted(SETTINGS)}")
    merged = {**base, **overlay["settings"]}
    rules = list(base["rules"])
    insert_at = None
    for rule in overlay["remove_rules"]:
        if rule not in rules:
            raise ValueError(f"rule to remove is not in the base policy: {json.dumps(rule)}")
        index = rules.index(rule)
        insert_at = index if insert_at is None else min(insert_at, index)
        rules.pop(index)
    if insert_at is None:
        insert_at = len(rules)
    merged["rules"] = rules[:insert_at] + list(overlay["add_rules"]) + rules[insert_at:]
    wait, ttl = merged["wait_seconds"], merged.get("approval_ttl_minutes", 5)
    if not 5 <= wait <= 10 or wait >= ttl * 60:
        raise ValueError("wait_seconds must be 5-10 and below approval_ttl_minutes * 60")
    return merged


def main(argv):
    if len(argv) not in (2, 3):
        sys.exit(__doc__)
    base = json.loads(Path(argv[1]).read_text())
    overlay = json.loads(Path(argv[2] if len(argv) == 3 else DEFAULT_OVERLAY).read_text())
    try:
        merged = apply_overlay(base, overlay)
    except ValueError as e:
        sys.exit(f"render_voice_policy: {e}")
    json.dump(merged, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main(sys.argv)
