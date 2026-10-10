#!/usr/bin/env python3
"""Print an ha-mcp policy document: BASE with the overlay in server/policy/voice_policy.proposed.json applied.

Usage: render_voice_policy.py BASE_POLICY.json [OVERLAY.json] > merged.json
Each `remove_rules` entry must match a base rule exactly; `add_rules` take the
position of the first removed rule. `wait_seconds` always comes from BASE. When no
removed rule is present and every added rule already is, the overlay counts as
applied: BASE is printed unchanged and "already applied" goes to stderr. Exits
non-zero on any other missing rule or an overlay that sets wait_seconds.
"""
import json
import sys
from pathlib import Path

DEFAULT_OVERLAY = Path(__file__).resolve().parents[1] / "policy" / "voice_policy.proposed.json"
SETTINGS = {"approval_ttl_minutes"}


def _validate(overlay: dict) -> None:
    if set(overlay) != {"settings", "remove_rules", "add_rules"}:
        raise ValueError("overlay keys must be exactly settings, remove_rules, add_rules")
    if "wait_seconds" in overlay["settings"]:
        raise ValueError("overlay must not change wait_seconds")
    if not set(overlay["settings"]) <= SETTINGS:
        raise ValueError(f"overlay settings must be a subset of {sorted(SETTINGS)}")


def is_applied(base: dict, overlay: dict) -> bool:
    _validate(overlay)
    rules = base["rules"]
    return (
        not any(rule in rules for rule in overlay["remove_rules"])
        and all(rule in rules for rule in overlay["add_rules"])
        and all(base.get(key) == value for key, value in overlay["settings"].items())
    )


def apply_overlay(base: dict, overlay: dict) -> dict:
    _validate(overlay)
    if is_applied(base, overlay):
        return base
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
    return merged


def main(argv):
    if len(argv) not in (2, 3):
        sys.exit(__doc__)
    base = json.loads(Path(argv[1]).read_text())
    overlay = json.loads(Path(argv[2] if len(argv) == 3 else DEFAULT_OVERLAY).read_text())
    try:
        applied = is_applied(base, overlay)
        merged = apply_overlay(base, overlay)
    except ValueError as e:
        sys.exit(f"render_voice_policy: {e}")
    if applied:
        print("render_voice_policy: already applied", file=sys.stderr)
    json.dump(merged, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main(sys.argv)
