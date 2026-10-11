"""weixue_agent_msg: wire builders, limits, notification validation and the delivery mapping."""
import json

import pytest

import weixue_agent_msg as wire


def item(**overrides):
    fields = {"chat_id": "c_7f3a", "task_id": "t_91b2", "state": "working", "summary": "Checking the lights"}
    fields.update(overrides)
    return fields


def test_contract_example_round_trips():
    out = wire.build_item(
        item(state="needs_approval", title="Living room", summary="Turn on living room lights", card_id="i_44c0",
             options=[{"id": "approve", "label": "Approve"}, {"id": "deny", "label": "Deny"}], risk="reversible",
             timeout_s=300, delivery="when_idle", speaks=True, debug_reply={"kind": "approve", "option_id": "approve"}),
        1739, 1791380000123,
    )
    assert out["kind"] == "agent" and out["v"] == 1 and out["seq"] == 1739 and out["ts"] == 1791380000123
    assert out["lang"] == "en" and out["speaks"] is True and out["timeout_s"] == 300
    assert out["debug_reply"] == {"kind": "approve", "option_id": "approve"}
    frame = json.loads(wire.frame(out))
    assert frame == {"type": "custom", "payload": out}


@pytest.mark.parametrize("state", wire.STATES)
def test_every_state_builds(state):
    assert wire.build_item(item(state=state), 1, 0)["state"] == state


@pytest.mark.parametrize(
    "overrides, error",
    [
        ({"state": "sleeping"}, "state"),
        ({"task_id": "t" * 37}, "task_id"),
        ({"chat_id": "c 1"}, "chat_id"),
        ({"summary": None}, "summary"),
        ({"delivery": "loud"}, "delivery"),
        ({"options": [{"id": "a", "label": "A"}]}, "card_id"),
        ({"options": [{"id": str(i), "label": "x"} for i in range(5)], "card_id": "i1"}, "options"),
        ({"state": "working", "risk": "money"}, "risk"),
        ({"state": "done", "progress": {"step": 1}}, "progress"),
        ({"playback": "played"}, "playback"),
        ({"debug_reply": {"kind": "maybe"}}, "debug_reply"),
        ({"surprise": 1}, "unknown"),
        ({"timeout_s": -1}, "timeout_s"),
        ({"speaks": "yes"}, "speaks"),
    ],
)
def test_invalid_items_rejected(overrides, error):
    with pytest.raises(wire.WireError) as err:
        wire.build_item(item(**overrides), 1, 0)
    assert error in str(err.value)


def test_texts_truncated_to_ui_limits():
    out = wire.build_item(
        item(state="needs_input", title="T" * 30, summary="灯" * 70, card_id="i1",
             options=[{"id": "a", "label": "Approve all lights"}, {"id": "b", "label": "批准所有的灯"}]),
        1, 0,
    )
    assert len(out["title"]) == 24 and out["summary"] == "灯" * 60 and out["lang"] == "zh"
    assert out["options"][0]["label"] == "Approve all " and out["options"][1]["label"] == "批准所有"


def test_silent_items_never_speak_and_size_limit():
    assert wire.build_item(item(delivery="silent", speaks=True), 1, 0)["speaks"] is False
    with pytest.raises(wire.WireError, match="1024"):
        wire.build_item(item(state="needs_input", card_id="i" * 36, title="标" * 24, summary="题" * 60,
                             options=[{"id": "o" * 32, "label": "选" * 4} for _ in range(4)],
                             debug_reply={"kind": "answer", "text": "答" * 200}), 2 ** 52, 0)


def test_sync():
    assert wire.build_sync(9, ["a", "b"]) == {"kind": "agent_sync", "v": 1, "seq": 9, "task_ids": ["a", "b"]}
    with pytest.raises(wire.WireError):
        wire.build_sync(9, ["bad id"])


def test_parse_response():
    params = {"v": 1, "boot": 2918841203, "seq": 12, "kind": "approve", "chat_id": "c_7f3a", "task_id": "t_91b2",
              "card_id": "i_44c0", "option_id": "approve", "via": "loopback"}
    out = wire.parse_response(params)
    assert out["kind"] == "approve" and out["via"] == "loopback" and out["boot"] == 2918841203
    assert wire.parse_response({"v": 1, "boot": 1, "seq": 2, "kind": "shown", "ack_upto": 1745})["ack_upto"] == 1745
    for bad in (
        dict(params, v=2),
        dict(params, kind="maybe"),
        {k: v for k, v in params.items() if k != "card_id"},
        dict(params, via="voice"),
        {"v": 1, "boot": 1, "seq": 2, "kind": "answer", "card_id": "i1"},
        {"v": 1, "boot": 1, "seq": 2, "kind": "answer", "card_id": "i1", "text": "x" * 121},
        {"v": 1, "boot": 1, "seq": 2, "kind": "shown"},
        dict(params, text="x" * 600),
    ):
        with pytest.raises(wire.WireError):
            wire.parse_response(bad)


def test_parse_played():
    cut = wire.parse_played({"v": 1, "boot": 1, "seq": 13, "sentence_id": "9f2c", "complete": False,
                             "cut_at_ms": 1840, "reason": "wake"})
    assert cut["cut_at_ms"] == 1840 and cut["reason"] == "wake"
    assert "cut_at_ms" not in wire.parse_played({"v": 1, "boot": 1, "seq": 14, "sentence_id": "9f2c", "complete": True})
    with pytest.raises(wire.WireError):
        wire.parse_played({"v": 1, "boot": 1, "seq": 13, "sentence_id": "9f2c", "complete": False, "reason": "wake"})
    with pytest.raises(wire.WireError):
        wire.parse_played({"v": 1, "boot": 1, "seq": 13, "sentence_id": "9f2c", "complete": False,
                           "cut_at_ms": 1, "reason": "bored"})


def test_decision_id_is_stable_per_device_boot_seq():
    assert wire.decision_id("d", 1, 2) == wire.decision_id("d", 1, 2) != wire.decision_id("d", 1, 3)


def test_from_delivery_maps_states_tiers_and_cards():
    offer = {
        "delivery_id": "dlv_1", "kind": "agent", "tier": "WHEN_IDLE", "speak": "I need your OK.", "chat_id": "cht_1",
        "task_id": "tsk_1",
        "payload": {"state": "needs_approval", "title": "Desk", "summary": "Turn on the desk plug", "lang": "en",
                    "interaction": {"interaction_id": "int_1", "expires_at": "2026-10-07T12:05:00Z",
                                    "action": {"text": "x", "tool": "ha_call_service", "reversible": True}}},
    }
    now = 1791374400.0  # 2026-10-07T12:00:00Z
    fields, speech = wire.from_delivery(offer, now=now)
    assert fields["state"] == "needs_approval" and fields["delivery"] == "when_idle" and fields["speaks"] is True
    assert fields["card_id"] == "int_1" and fields["timeout_s"] == 300 and fields["risk"] == "reversible"
    assert [o["id"] for o in fields["options"]] == ["approve", "deny"] and speech == "I need your OK."
    wire.build_item(fields, 1, 0)

    progress = {"kind": "agent", "tier": "SILENT", "speak": None, "chat_id": "cht_1", "task_id": "tsk_1",
                "payload": {"state": "progress", "summary": "Working", "progress": {"step": 2, "total": None}}}
    fields, speech = wire.from_delivery(progress, now=now)
    assert fields["state"] == "working" and fields["progress"] == {"step": 2, "total": None} and speech is None
    assert fields["delivery"] == "silent" and fields["speaks"] is False

    done = {"kind": "agent", "tier": "SILENT", "chat_id": "cht_1", "task_id": "tsk_1",
            "payload": {"state": "done", "summary": "Done", "playback": "uncertain", "unread": True}}
    fields, _ = wire.from_delivery(done, now=now)
    assert fields["state"] == "unread" and fields["playback"] == "uncertain" and fields["seen"] is False


def test_response_routes():
    base = {"boot": 1, "seq": 2, "chat_id": "cht_1", "card_id": "int_1"}
    method, path, body = wire.response_route("dev", dict(base, kind="approve"))
    assert (method, path) == ("POST", "/v1/chats/cht_1/approve") and body["interaction_id"] == "int_1"
    assert body["decision_id"] == wire.decision_id("dev", 1, 2) and body["via"] == "panel"
    assert wire.response_route("dev", dict(base, kind="answer", option_id="o1"))[2]["option_ids"] == ["o1"]
    assert wire.response_route("dev", dict(base, kind="answer", text="blue"))[2]["text"] == "blue"
    assert wire.response_route("dev", dict(base, kind="cancel"))[1:] == (
        "/v1/chats/cht_1/interrupt", {"decision_id": wire.decision_id("dev", 1, 2), "reason": "panel"})
    assert wire.response_route("dev", dict(base, kind="ack_seen"))[1] == "/v1/chats/cht_1/read"
    assert wire.response_route("dev", dict(base, kind="new_chat"))[1] == "/v1/chats"
    assert wire.response_route("dev", dict(base, kind="shown", ack_upto=3)) is None


def test_ids_exclude_url_delimiters_and_routes_quote_the_chat():
    params = {"v": 1, "boot": 1, "seq": 2, "kind": "cancel", "task_id": "t1"}
    for bad in ("c/../admin", "c?x=1", "c#frag", "c d", "x" * 37, "ç"):
        with pytest.raises(wire.WireError):
            wire.parse_response(dict(params, chat_id=bad))
        with pytest.raises(wire.WireError):
            wire.response_route("dev", {"boot": 1, "seq": 2, "kind": "cancel", "chat_id": bad})
        with pytest.raises(wire.WireError):
            wire.build_item({"chat_id": bad, "task_id": "t1", "state": "working", "summary": "s"}, 1, 0)
    assert wire.parse_response(dict(params, chat_id="c_7f3a-:@.~"))["chat_id"] == "c_7f3a-:@.~"
    route = wire.response_route("dev", {"boot": 1, "seq": 2, "kind": "cancel", "chat_id": "a%2Fb&c=d"})
    assert route[1] == "/v1/chats/a%252Fb%26c%3Dd/interrupt"


# agent_chats (v1.2)

def chat(cid, title="Living room", state="idle", age_s=60):
    return {"chat_id": cid, "title": title, "state": state, "age_s": age_s}


def test_chats_payload_shape_and_normalisation():
    out = wire.build_chats(1746, 1791380000123, "cht_a",
                           [chat("cht_a", "A very long chat title that goes on", "needs_you"),
                            chat("cht_b", None, "sleeping", -5)])
    assert out == {"kind": "agent_chats", "v": 1, "seq": 1746, "ts": 1791380000123, "current": "cht_a", "chats": [
        {"id": "cht_a", "title": "A very long chat title t", "state": "needs_you", "age_s": 60},
        {"id": "cht_b", "state": "idle", "age_s": 0}]}
    assert "current" not in wire.build_chats(1, 0, None, [])
    with pytest.raises(wire.WireError):
        wire.build_chats(1, 0, None, [chat("bad id")])


def test_chats_payload_fits_the_item_limit_and_keeps_the_current_chat():
    chats = [chat(f"cht_01JXXXXXXXXXXXXXXXXXXXXXX{i}", "客厅灯光和窗帘的晚间场景设置以及其他一些长长的说明", "working", 864000)
             for i in range(12)]
    out = wire.build_chats(2 ** 40, 2 ** 40, chats[0]["chat_id"], chats)
    assert wire.size(out) <= wire.MAX_ITEM_BYTES and 4 <= len(out["chats"]) < 12
    assert out["chats"][0]["id"] == chats[0]["chat_id"]
    ascii_chats = [chat(f"cht_01JXXXXXXXXXXXXXXXXXXXXXX{i}", "x" * 24, "needs_you", 864000) for i in range(12)]
    assert len(wire.build_chats(2 ** 40, 2 ** 40, None, ascii_chats)["chats"]) >= 7


def test_chats_from_listing_orders_current_first_and_falls_back_to_task_titles():
    body = {"current_chat_id": "cht_b", "items": [
        {"chat_id": "cht_a", "title": "New chat", "title_source": "placeholder", "state": "idle",
         "last_activity_at": "2026-09-21T12:00:00Z"},
        {"chat_id": "cht_b", "title": "Lights", "title_source": "agent", "state": "needs_you",
         "last_activity_at": "2026-09-21T12:59:00+00:00"},
        {"chat_id": "bad id", "title": "x"},
    ]}
    now = 1790000000 - 5060 + 3600  # 2026-09-21T13:49:00Z
    current, chats = wire.chats_from_listing(body, now, {"cht_a": "Desk lamp"})
    assert current == "cht_b"
    assert chats == [{"chat_id": "cht_b", "title": "Lights", "state": "needs_you", "age_s": 3000},
                     {"chat_id": "cht_a", "title": "Desk lamp", "state": "idle", "age_s": 6540}]
    assert wire.chats_from_listing({"current_chat_id": "cht_gone", "items": []}, now) == (None, [])
