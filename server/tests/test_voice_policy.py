"""Checks server/policy/voice_policy.proposed.json against a model of ha-mcp 8.6.0
require_approval evaluation (`held`, after src/ha_mcp/policy/evaluator.py)."""
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
OVERLAY = json.loads((SERVER_DIR / "policy" / "voice_policy.proposed.json").read_text())
FIXTURES = Path(__file__).resolve().parent / "fixtures"
HA_AGENT_PRE = json.loads((FIXTURES / "ha_agent_policy_pre_overlay.json").read_text())
HA_AGENT_POST = json.loads((FIXTURES / "ha_agent_policy_post_overlay.json").read_text())

_spec = importlib.util.spec_from_file_location("render_voice_policy", SERVER_DIR / "scripts" / "render_voice_policy.py")
render = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render)

BASE = {
    "wait_seconds": 180,
    "approval_ttl_minutes": 10,
    "version": 0,
    "rules": [
        {"tool_name": "ha_bulk_control", "when": [], "remember_minutes": 0},
        {
            "tool_name": "ha_call_service",
            "when": [{"path": "args.domain", "op": "not_in", "value": ["light", "notify"]}],
            "remember_minutes": 0,
        },
        {"tool_name": "ha_restart", "when": [], "remember_minutes": 0},
    ],
}
MERGED = render.apply_overlay(BASE, OVERLAY)


def _values(args, path):
    parts = path.split(".")
    if parts[0] == "args":
        parts = parts[1:]

    def walk(cur, rest):
        if not rest:
            yield cur
            return
        head, tail = rest[0], rest[1:]
        if head == "*":
            if isinstance(cur, dict):
                children = list(cur.values())
            elif isinstance(cur, (list, tuple)):
                children = list(cur)
            else:
                children = None
        elif isinstance(cur, dict) and head in cur:
            children = [cur[head]]
        else:
            children = None
        for child in children or []:
            yield from walk(child, tail)

    return list(walk(args, parts))


def _ci(x):
    return x.lower() if isinstance(x, str) else x


def _op(value, op, expected):
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
    raise AssertionError(f"op {op} not modelled")


def _predicate(predicate, args):
    values = _values(args, predicate["path"])
    if not values:
        return False
    if predicate["op"] == "exists":
        return True
    return any(_op(v, predicate["op"], predicate["value"]) for v in values)


def held(policy, tool, args):
    for rule in policy["rules"]:
        if rule["tool_name"] in ("*", tool) and all(_predicate(p, args) for p in rule["when"]):
            return True
    return (
        tool == "ha_call_service"
        and bool(args.get("ws_command"))
        and any(r["tool_name"] in ("ha_call_service", "*") for r in policy["rules"])
    )


def call(domain, service, entity_id=None, data=None):
    args = {"domain": domain, "service": service}
    if entity_id is not None:
        args["entity_id"] = entity_id
    if data is not None:
        args["data"] = data
    return args


MEROSS = "switch.smart_switch_24110844476400510301c4e7ae0f5a9f_outlet"

FREE = [
    call("light", "turn_off", "light.living_room_ceiling_lights"),
    call("notify", "notify", data={"message": "hi"}),
    call("switch", "turn_on", MEROSS),
    call("Switch", "toggle", "switch.living_room_donut_light_outlet"),
    call("cover", "set_cover_position", "cover.curtain_partner_1", {"position": 40}),
    call("cover", "close_cover", "cover.curtain_partner_1"),
    call("media_player", "volume_set", "media_player.tv", {"volume_level": 0.3}),
    call("climate", "set_temperature", "climate.living_room", {"temperature": 21}),
    call("climate", "set_temperature", "climate.living_room", {"target_temp_low": 18, "target_temp_high": 24}),
    call("climate", "set_hvac_mode", "climate.living_room", {"hvac_mode": "heat"}),
    call("climate", "turn_off", "climate.living_room"),
]

HELD = [
    call("switch", "turn_on", "switch.working_desk_plug"),
    call("switch", "turn_on", "switch.zigbee2mqtt_bridge_permit_join"),
    call("switch", "turn_on", f"{MEROSS},switch.working_desk_plug"),
    call("switch", "turn_on", MEROSS, {"entity_id": "switch.zigbee2mqtt_bridge_permit_join"}),
    call("switch", "turn_off", data={"area_id": "living_room"}),
    call("cover", "open_cover", "cover.garage_door"),
    call("cover", "open_cover", "cover.curtain_partner_1", {"device_id": "abc"}),
    call("cover", "open_cover", data={"label_id": "all_covers"}),
    call("climate", "set_temperature", "climate.living_room", {"temperature": 30}),
    call("climate", "set_temperature", "climate.living_room", {"temperature": 10}),
    call("climate", "set_temperature", "climate.living_room", {"temperature": "21"}),
    call("climate", "set_temperature", "climate.living_room", {"target_temp_high": 28}),
    call("climate", "set_preset_mode", "climate.living_room", {"preset_mode": "boost"}),
    call("lock", "unlock", "lock.front_door"),
    call("alarm_control_panel", "alarm_disarm", "alarm_control_panel.entrance_doorbell_security_system"),
    call("hassio", "addon_stop", data={"addon": "core_ssh"}),
    call("homeassistant", "restart"),
    call("homeassistant", "turn_off", "switch.working_desk_plug"),
    call("shell_command", "anything"),
    call("python_script", "anything"),
    call("script", "turn_on", "script.start_gaming"),
    {"ws_command": "config/auth/list", "data": {}},
]


@pytest.mark.parametrize("args", FREE, ids=lambda a: f"{a.get('domain')}.{a.get('service')}")
def test_voice_free(args):
    assert not held(MERGED, "ha_call_service", args)


@pytest.mark.parametrize("args", HELD, ids=lambda a: f"{a.get('domain')}.{a.get('service')}")
def test_voice_held(args):
    assert held(MERGED, "ha_call_service", args)


def test_settings_and_untouched_rules():
    assert MERGED["wait_seconds"] == BASE["wait_seconds"]
    assert OVERLAY["settings"] == {}
    assert MERGED["approval_ttl_minutes"] == 10
    assert MERGED["rules"][0] == BASE["rules"][0] and MERGED["rules"][-1] == BASE["rules"][-1]
    assert BASE["rules"][1] not in MERGED["rules"]
    assert all(r["remember_minutes"] == 0 for r in MERGED["rules"])


def test_overlay_rejects_missing_rule():
    base = {**BASE, "rules": [BASE["rules"][0]]}
    with pytest.raises(ValueError, match="not in the base policy"):
        render.apply_overlay(base, OVERLAY)


@pytest.mark.parametrize("wait", [8, 60, 180])
def test_overlay_rejects_any_wait_seconds(wait):
    with pytest.raises(ValueError, match="must not change wait_seconds"):
        render.apply_overlay(BASE, {**OVERLAY, "settings": {"wait_seconds": wait}})


def test_overlay_applies_to_ha_agent_policy():
    base = HA_AGENT_PRE
    assert len(base["rules"]) == 87
    merged = render.apply_overlay(base, OVERLAY)
    others = [r for r in base["rules"] if r["tool_name"] != "ha_call_service"]
    assert [r for r in merged["rules"] if r["tool_name"] != "ha_call_service"] == others
    assert merged["wait_seconds"] == base["wait_seconds"]
    assert merged["rules"] == HA_AGENT_POST["rules"]
    assert held(merged, "ha_call_service", call("lock", "unlock", "lock.front_door"))
    assert not held(merged, "ha_call_service", call("switch", "turn_on", MEROSS))


def test_overlay_already_applied_leaves_base_unchanged():
    assert render.is_applied(HA_AGENT_POST, OVERLAY)
    assert not render.is_applied(HA_AGENT_PRE, OVERLAY)
    assert render.apply_overlay(HA_AGENT_POST, OVERLAY) == HA_AGENT_POST
    assert held(HA_AGENT_POST, "ha_call_service", call("lock", "unlock", "lock.front_door"))
    assert not held(HA_AGENT_POST, "ha_call_service", call("switch", "turn_on", MEROSS))


def test_overlay_rejects_partially_applied_base():
    rules = [r for r in HA_AGENT_POST["rules"] if r not in OVERLAY["add_rules"][1:]]
    with pytest.raises(ValueError, match="not in the base policy"):
        render.apply_overlay({**HA_AGENT_POST, "rules": rules}, OVERLAY)


def test_overlay_already_applied_still_rejects_wait_seconds():
    with pytest.raises(ValueError, match="must not change wait_seconds"):
        render.apply_overlay(HA_AGENT_POST, {**OVERLAY, "settings": {"wait_seconds": 8}})


def test_cli_reports_already_applied(tmp_path):
    base = tmp_path / "base.json"
    base.write_text(json.dumps(HA_AGENT_POST))
    script = SERVER_DIR / "scripts" / "render_voice_policy.py"
    done = subprocess.run([sys.executable, str(script), str(base)], capture_output=True, text=True)
    assert done.returncode == 0
    assert "already applied" in done.stderr
    assert json.loads(done.stdout) == HA_AGENT_POST


def _bridge_domains(name):
    return set(json.loads((SERVER_DIR / "policy" / name).read_text())["call_service_domains"])


def test_live_bridge_domains_are_free_under_current_policy():
    for domain in _bridge_domains("voice_bridge.json"):
        assert not held(BASE, "ha_call_service", call(domain, "turn_on"))
    for domain in _bridge_domains("voice_bridge.json"):
        assert not held(HA_AGENT_PRE, "ha_call_service", call(domain, "turn_on"))


def test_proposed_bridge_domains_match_overlay():
    free_domains = {a["domain"].lower() for a in FREE} - {"notify"}
    assert _bridge_domains("voice_bridge.proposed.json") == free_domains
