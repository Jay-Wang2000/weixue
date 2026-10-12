"""Chat index routes (feat-ui-sessions)."""


async def test_chat_list_switch_back_resolve_and_read(make):
    h = await make()
    first = await h.send_json("Kitchen lights tonight", origin="api")
    await h.idle(first["chat_id"])
    created = (await h.http.post("/v1/chats", json={"device_id": "sim-device-01"},
                                 headers={"Idempotency-Key": "c-1"})).json()
    second = await h.send_json("Weather tomorrow", chat=created["chat_id"])
    await h.idle(second["chat_id"])
    listing = (await h.http.get("/v1/chats", params={"device_id": "sim-device-01"})).json()
    assert listing["current_chat_id"] == created["chat_id"] and listing["previous_chat_id"] == first["chat_id"]
    assert listing["items"][0]["chat_id"] == created["chat_id"]
    assert {c["title"] for c in listing["items"]} == {"Kitchen lights tonight", "Weather tomorrow"}
    switched = (await h.http.post(f"/v1/chats/{first['chat_id']}/switch")).json()
    assert switched["current_chat_id"] == first["chat_id"] and switched["last_summary"] == "Hello there."
    back = (await h.http.post("/v1/chats/back", json={"device_id": "sim-device-01"})).json()
    assert back["current_chat_id"] == created["chat_id"]
    resolved = (await h.http.get("/v1/chats/resolve", params={"device_id": "sim-device-01", "q": "kitchen"})).json()
    assert resolved["matches"][0]["chat_id"] == first["chat_id"]
    detail = (await h.http.get(f"/v1/chats/{first['chat_id']}")).json()
    assert detail["state"] == "unread" and detail["turns"][0]["state"] == "completed"
    read = (await h.http.post(f"/v1/chats/{first['chat_id']}/read")).json()
    assert read["state"] == "idle" and read["unread"] is False


async def test_auth_is_required_except_healthz(make):
    h = await make()
    import httpx
    from weixue_agent.api import Api
    anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=Api(h.service).app()), base_url="http://agent")
    assert (await anon.get("/v1/status")).status_code == 401
    assert (await anon.get("/healthz")).status_code in (200, 503)
    await anon.aclose()
