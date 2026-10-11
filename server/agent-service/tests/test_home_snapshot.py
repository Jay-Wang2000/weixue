"""Home snapshot: selection, override list, write-on-change, failure handling, free reads, schema v3."""
import asyncio
import json
from datetime import timedelta

import pytest

from conftest import TOKEN
from fake_hamcp import FakeHaMcp
from weixue_agent import home as home_mod, timeutil
from weixue_agent.config import ConfigError, load_settings
from weixue_agent.home import HOME_ENTITIES_MAX, HomeSnapshot

BASE_ENV = {"AGENT_SERVICE_TOKEN": TOKEN, "AGENT_DATA_DIR": "/tmp/x"}


def _entity(fake, entity_id, state="off", name=None, changed="2026-10-10T23:00:00+01:00", **attributes):
    fake.home[entity_id] = {"state": state, "last_changed": changed,
                            "attributes": {"friendly_name": name or entity_id, **attributes}}


def _rows(h):
    return {r["entity_id"]: r for r in h.store.all("SELECT * FROM home_snapshot ORDER BY entity_id")}


def _meta(h):
    return h.store.one("SELECT * FROM home_snapshot_meta WHERE id = 1")


async def test_default_selection_is_capped_with_locks_first_and_stable_order(make):
    fake = FakeHaMcp(wait_seconds=2.0)
    for i in range(25):
        _entity(fake, f"light.l{i:02d}")
    _entity(fake, "lock.front_door", "locked")
    _entity(fake, "climate.house", "heat", current_temperature=19.5, temperature=21, hvac_action="heating", secret="x")
    _entity(fake, "cover.curtain", "open", current_position=36)
    _entity(fake, "sensor.outside", "11")
    h = await make(fake_ha=fake)
    assert await h.service.home.run_once() is True
    rows = _rows(h)
    assert len(rows) == HOME_ENTITIES_MAX == 20
    assert set(rows) == {"lock.front_door", "climate.house", "cover.curtain"} | {f"light.l{i:02d}" for i in range(17)}
    request = [c for c in fake.calls if c["tool"] == "ha_get_state"][0]["args"]
    assert request["entity_id"] == ["lock.front_door", "climate.house", "cover.curtain"] + \
        [f"light.l{i:02d}" for i in range(17)]
    assert json.loads(rows["climate.house"]["attributes_json"]) == \
        {"current_temperature": 19.5, "temperature": 21, "hvac_action": "heating"}
    assert rows["cover.curtain"]["name"] == "cover.curtain" and rows["lock.front_door"]["state"] == "locked"
    first = [r["entity_id"] for r in h.store.all("SELECT entity_id FROM home_snapshot ORDER BY rowid")]
    await h.service.home.run_once()
    assert [r["entity_id"] for r in h.store.all("SELECT entity_id FROM home_snapshot ORDER BY rowid")] == first


async def test_reads_are_free_and_use_the_gateway(make):
    fake = FakeHaMcp(wait_seconds=2.0)
    _entity(fake, "light.lamp")
    h = await make(fake_ha=fake)
    await h.service.home.run_once()
    assert [c["tool"] for c in fake.calls[-2:]] == ["ha_get_overview", "ha_get_state"]
    assert not fake.queue and h.service.ha.open_holds() == []
    states = h.store.all("SELECT tool, state FROM approvals WHERE tool IN ('ha_get_overview', 'ha_get_state')")
    assert {(r["tool"], r["state"]) for r in states} == {("ha_get_overview", "executed"), ("ha_get_state", "executed")}
    assert h.store.all("SELECT * FROM interactions") == []
    assert h.service.home.held == 0


async def test_override_list_replaces_enumeration_and_is_capped(make):
    fake = FakeHaMcp(wait_seconds=2.0)
    _entity(fake, "light.a")
    _entity(fake, "sensor.temp", "11", unit_of_measurement="C", noise=1)
    h = await make(fake_ha=fake)
    wanted = ("sensor.temp", "light.a", "light.missing")
    snapshot = HomeSnapshot(h.store, h.service.ha, 300, wanted)
    assert await snapshot.run_once() is True
    assert [c["tool"] for c in fake.calls if c["tool"].startswith("ha_get_")] == ["ha_get_state"]
    rows = _rows(h)
    assert set(rows) == {"sensor.temp", "light.a"}
    assert json.loads(rows["sensor.temp"]["attributes_json"]) == {"unit_of_measurement": "C"}
    assert _meta(h)["entity_count"] == 2


def test_override_list_parsing_and_limits():
    ids = ",".join(f"light.l{i}" for i in range(30))
    settings = load_settings({**BASE_ENV, "AGENT_HOME_ENTITIES": " Light.A , light.a,sensor.b,, " + ids})
    assert settings.home_entities[:2] == ("light.a", "sensor.b") and len(settings.home_entities) == 20
    assert load_settings(BASE_ENV).home_entities == ()
    with pytest.raises(ConfigError):
        load_settings({**BASE_ENV, "AGENT_HOME_ENTITIES": "light.a,not an entity"})


def test_period_defaults_and_validation():
    assert load_settings(BASE_ENV).home_snapshot_s == 300
    assert load_settings({**BASE_ENV, "AGENT_HOME_SNAPSHOT_S": "60"}).home_snapshot_s == 60
    assert load_settings({**BASE_ENV, "AGENT_HOME_SNAPSHOT_S": "0"}).home_snapshot_s == 0
    for bad in ("soon", "-1"):
        with pytest.raises(ConfigError):
            load_settings({**BASE_ENV, "AGENT_HOME_SNAPSHOT_S": bad})


async def test_rows_are_written_only_on_change(make):
    fake = FakeHaMcp(wait_seconds=2.0)
    _entity(fake, "light.a")
    _entity(fake, "light.b")
    h = await make(fake_ha=fake)
    home = h.service.home
    await home.run_once()
    first = _rows(h)
    meta_first = _meta(h)
    await asyncio.sleep(0.01)
    await home.run_once()
    assert _rows(h) == first
    assert _meta(h)["fetched_at"] > meta_first["fetched_at"]
    fake.home["light.b"]["state"] = "on"
    await asyncio.sleep(0.01)
    await home.run_once()
    second = _rows(h)
    assert second["light.a"] == first["light.a"]
    assert second["light.b"]["state"] == "on" and second["light.b"]["updated_at"] > first["light.b"]["updated_at"]


async def test_entities_that_leave_the_selection_are_removed(make):
    fake = FakeHaMcp(wait_seconds=2.0)
    _entity(fake, "light.a")
    _entity(fake, "light.b")
    h = await make(fake_ha=fake)
    await h.service.home.run_once()
    del fake.home["light.b"]
    await h.service.home.run_once()
    assert set(_rows(h)) == {"light.a"} and _meta(h)["entity_count"] == 1


async def test_a_failed_cycle_keeps_the_rows_records_the_code_once_and_never_raises(make):
    fake = FakeHaMcp(wait_seconds=2.0)
    _entity(fake, "light.a")
    h = await make(fake_ha=fake)
    home = h.service.home
    await home.run_once()
    before, meta = _rows(h), _meta(h)
    fake.down = True
    assert await home.run_once() is False
    assert await home.run_once() is False
    assert _rows(h) == before
    failed = _meta(h)
    assert failed["error_code"] == "ha_error"
    assert failed["fetched_at"] == meta["fetched_at"] and failed["entity_count"] == 1
    assert len(h.store.all("SELECT * FROM errors WHERE source = 'home_snapshot'")) == 1
    fake.down = False
    assert await home.run_once() is True
    assert _meta(h)["error_code"] is None and _meta(h)["fetched_at"] > meta["fetched_at"]


async def test_an_unreachable_ha_mcp_is_its_own_code(make):
    fake = FakeHaMcp(wait_seconds=2.0)
    _entity(fake, "light.a")
    h = await make(fake_ha=fake)
    h.transport.refuse = {"private_test"}
    assert await h.service.home.run_once() is False
    assert _meta(h)["error_code"] == "ha_unreachable" and _rows(h) == {}


async def test_a_malformed_answer_is_a_failed_cycle(make, monkeypatch):
    fake = FakeHaMcp(wait_seconds=2.0)
    _entity(fake, "light.a")
    h = await make(fake_ha=fake)
    original = fake._run

    def garbled(tool, args):
        out = original(tool, args)
        if tool == "ha_get_overview":
            out["content"][0]["text"] = "not json"
        return out

    monkeypatch.setattr(fake, "_run", garbled)
    assert await h.service.home.run_once() is False
    assert _meta(h)["error_code"] == "bad_response" and _rows(h) == {}


async def test_a_held_read_fails_the_cycle_and_creates_no_task_card(make):
    fake = FakeHaMcp(wait_seconds=2.0)
    fake.rules = [{"tool_name": "ha_get_overview", "when": [], "remember_minutes": 0}]
    _entity(fake, "light.a")
    h = await make(fake_ha=fake)
    assert await h.service.home.run_once() is False
    assert _meta(h)["error_code"] == "held" and h.service.home.held == 1
    assert h.store.all("SELECT * FROM interactions") == [] and _rows(h) == {}


async def test_names_and_attributes_are_redacted_before_they_are_stored(make):
    fake = FakeHaMcp(wait_seconds=2.0)
    _entity(fake, "light.a", name="Lamp http://ha.local:9584/private_SECRETPATH")
    h = await make(fake_ha=fake)
    await h.service.home.run_once()
    assert "private_SECRETPATH" not in json.dumps(_rows(h))


async def test_the_loop_runs_the_job_on_its_period(make, monkeypatch):
    monkeypatch.setattr(home_mod, "FIRST_RUN_DELAY_S", 0)
    fake = FakeHaMcp(wait_seconds=2.0)
    _entity(fake, "light.a")
    h = await make(fake_ha=fake)
    loop_job = HomeSnapshot(h.store, h.service.ha, 0.2)
    loop_job.start()
    try:
        await h.wait_for(lambda: _meta(h) and _meta(h)["fetched_at"], what="first snapshot")
        first = _meta(h)["fetched_at"]
        await h.wait_for(lambda: _meta(h)["fetched_at"] > first, what="second snapshot")
    finally:
        await loop_job.stop()
    assert _meta(h)["period_s"] == 0.2


async def test_period_zero_starts_no_job(make):
    h = await make()
    job = HomeSnapshot(h.store, h.service.ha, 0)
    job.start()
    assert job._task is None


async def test_prune_drops_old_terminal_approvals_only(make):
    h = await make()
    old = timeutil.iso(timeutil.now() - timedelta(days=40))
    for approval_id, state in (("a1", "executed"), ("a2", "unknown"), ("a3", "pending")):
        h.store.insert("approvals", {"approval_id": approval_id, "tool": "ha_get_state", "args_json": "{}",
                                     "args_hash": approval_id, "state": state, "created_at": old, "updated_at": old})
    h.store.prune(30)
    assert {r["approval_id"] for r in h.store.all("SELECT approval_id FROM approvals")} == {"a2", "a3"}
