"""Simulated device for the proactive flow (docs/proactive.md): long-polls, opens a session on 200,
streams silence and optionally a 16 kHz mono WAV reply after the first `tts stop` (with
--reply-during-playback also after the first audio frame), and prints the server's messages.
--direct opens a session without polling, as a user-started session; --wake TEXT then sends a
wake-word `listen detect` with TEXT. After a poll's 200 the invocation is `weixue:proactive`, as the
firmware sends it, or with --bind `weixue:proactive:<id>:<lease>` from the poll answer;
--invoke-delay waits before opening the session. --abort-after S sends `abort` S seconds after the
detect. Requests carry Device-Id and Client-Id, plus Authorization when --token is given.
--mcp-tools answers the server's device MCP (initialize, tools/list with SIM_TOOLS, tools/call, each
call logged as `mcp call <name> <args>`). --replies N sends the reply WAV after each of the first N
`tts stop`s; --barge-in S sends it once more (its first --barge-frames frames) S seconds after the first
audio frame that follows the first reply (while the answer plays); --agent adds `features.agent` to the hello and answers a `tts stop` that
carries `ack` with a complete `notifications/weixue/played` (logged as `played <sentence_id>`)."""
import argparse
import asyncio
import json
import time
import uuid
import wave

import aiohttp
import opuslib_next
import websockets

FRAME = 960  # 60 ms at 16 kHz

def frames_from_wav(path):
    with wave.open(path) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        pcm = w.readframes(w.getnframes())
    pcm += b"\0" * (-len(pcm) % (FRAME * 2))
    return [pcm[i:i + FRAME * 2] for i in range(0, len(pcm), FRAME * 2)]

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

SIM_TOOLS = [
    {"name": "self.get_device_status", "description": "Device status.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "self.audio_speaker.set_volume", "description": "Set the volume.",
     "inputSchema": {"type": "object", "properties": {"volume": {"type": "integer"}}, "required": ["volume"]}},
    {"name": "self.screen.set_bedside", "description": "Bedside screen: mode night|day|auto|status.",
     "inputSchema": {"type": "object", "properties": {"mode": {"type": "string"}}}},
    {"name": "self.diag.gesture", "description": "Operator-only injection.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
]

def mcp_result(payload):
    """The JSON-RPC result for a server request, or None when it needs no answer."""
    method = payload.get("method")
    if method == "initialize":
        return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "sim-device", "version": "1"}}
    if method == "tools/list":
        return {"tools": SIM_TOOLS}
    if method == "tools/call":
        params = payload.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        log("mcp call", name, json.dumps(args, sort_keys=True))
        if name == "self.screen.set_bedside":
            mode = args.get("mode", "status")
            state = {"night": mode == "night", "showing": False, "hint": mode, "window": "22:00-07:00"}
        elif name == "self.audio_speaker.set_volume":
            state = {"volume": args.get("volume")}
        else:
            state = {"ok": True}
        return {"content": [{"type": "text", "text": json.dumps(state)}], "isError": False}
    return None

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18003")
    ap.add_argument("--ws", default="ws://127.0.0.1:18000/xiaozhi/v1/")
    ap.add_argument("--device", default="aa:bb:cc:dd:ee:01")
    ap.add_argument("--reply", default=None)
    ap.add_argument("--reply-delay", type=float, default=1.0)
    ap.add_argument("--reply-during-playback", action="store_true")
    ap.add_argument("--client-id", default=str(uuid.uuid4()))
    ap.add_argument("--token", default=None, help="device token from the OTA response")
    ap.add_argument("--polls", type=int, default=0, help="give up after this many polls (0: no limit)")
    ap.add_argument("--max-seconds", type=float, default=40)
    ap.add_argument("--direct", action="store_true", help="open a session without polling (user-started session)")
    ap.add_argument("--invoke", action="store_true", help="with --direct, still send the proactive listen detect")
    ap.add_argument("--wake", default=None, help="with --direct, send a wake-word listen detect with this text")
    ap.add_argument("--bind", action="store_true", help="invoke with the id and lease of the poll answer")
    ap.add_argument("--invoke-delay", type=float, default=0, help="seconds between the poll's 200 and the session")
    ap.add_argument("--abort-after", type=float, default=None, help="send abort this many seconds after the detect")
    ap.add_argument("--mcp-tools", action="store_true", help="answer device MCP requests with SIM_TOOLS")
    ap.add_argument("--replies", type=int, default=1, help="send the reply after each of the first N tts stops")
    ap.add_argument("--barge-in", type=float, default=None,
                    help="send the reply again this many seconds into the answer to the first reply")
    ap.add_argument("--barge-frames", type=int, default=None, help="with --barge-in, send only this many frames")
    ap.add_argument("--agent", action="store_true", help="advertise features.agent in the hello")
    a = ap.parse_args()
    ident = {"Device-Id": a.device, "Client-Id": a.client_id}
    if a.token:
        ident["Authorization"] = f"Bearer {a.token}"
    invoke_text = "weixue:proactive"
    async with aiohttp.ClientSession() as http:
        polls = 0
        while not a.direct:
            async with http.get(a.base + "/weixue/proactive/poll", headers=ident) as r:
                body = await r.text()
                log("poll", r.status, body.strip())
                polls += 1
                if r.status == 200:
                    if a.bind:
                        answer = json.loads(body)
                        invoke_text = f"weixue:proactive:{answer['id']}:{answer['lease']}"
                    break
                if a.polls and polls >= a.polls:
                    return
                if r.status not in (200, 204):
                    await asyncio.sleep(1)
    if a.invoke_delay:
        log("waiting", a.invoke_delay, "s before opening the session")
        await asyncio.sleep(a.invoke_delay)
    enc = opuslib_next.Encoder(16000, 1, "voip")
    silence = enc.encode(b"\0" * FRAME * 2, FRAME)
    reply = [enc.encode(f, FRAME) for f in frames_from_wav(a.reply)] if a.reply else []
    headers = {**ident, "Protocol-Version": "1"}
    async with websockets.connect(a.ws, additional_headers=headers) as ws:
        features = {"mcp": True, **({"agent": 1} if a.agent else {})}
        await ws.send(json.dumps({"type": "hello", "version": 1, "features": features, "transport": "websocket",
                                  "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}))
        first = await ws.recv()
        try:
            hello = json.loads(first)
        except ValueError:
            log("refused:", first)
            return
        if hello.get("type") != "hello":
            log("refused:", first)
            return
        sid = hello.get("session_id")
        log("hello", sid)
        if not a.direct or a.invoke:
            await ws.send(json.dumps({"session_id": sid, "type": "listen", "state": "detect", "text": invoke_text}))
        elif a.wake:
            await ws.send(json.dumps({"session_id": sid, "type": "listen", "state": "detect", "text": a.wake}))
        await ws.send(json.dumps({"session_id": sid, "type": "listen", "state": "start", "mode": "realtime"}))
        if a.abort_after is not None:
            await asyncio.sleep(a.abort_after)
            log("sending abort")
            await ws.send(json.dumps({"session_id": sid, "type": "abort"}))
        state = {"stops": 0, "reply_at": None, "audio": 0, "clips": 0, "barge": a.barge_in is not None, "events": 0}
        boot = int(time.time()) % 2 ** 32
        async def sender():
            queue = []
            while not ws.close_code:
                if state["reply_at"] and time.time() >= state["reply_at"] and reply:
                    queue, state["reply_at"] = list(reply[:state.pop("frames", None)]), None
                    state["clips"] += 1
                    log("sending reply clip", len(queue), "frames")
                try:
                    await ws.send(queue.pop(0) if queue else silence)
                except websockets.ConnectionClosed:
                    return
                await asyncio.sleep(0.06)
        async def receiver():
            async for msg in ws:
                if isinstance(msg, bytes):
                    state["audio"] += 1
                    if state["audio"] == 1 and reply and a.reply_during_playback:
                        state["reply_at"] = time.time() + a.reply_delay
                    if state["barge"] and state["clips"] == 1 and not state["reply_at"]:
                        state["barge"] = False
                        state["reply_at"] = time.time() + a.barge_in
                        state["frames"] = a.barge_frames
                        log("barge-in in", a.barge_in, "s")
                    continue
                m = json.loads(msg)
                if m.get("type") == "mcp":
                    payload = m.get("payload") or {}
                    result = mcp_result(payload) if a.mcp_tools else None
                    if result is not None:
                        await ws.send(json.dumps({"session_id": sid, "type": "mcp",
                                                  "payload": {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}}))
                    continue
                log("<<", json.dumps({k: v for k, v in m.items() if k != "session_id"}, ensure_ascii=False), f"(audio frames so far {state['audio']})")
                if m.get("type") == "tts" and m.get("state") == "stop":
                    state["stops"] += 1
                    if a.agent and m.get("ack") and m.get("sentence_id"):
                        state["events"] += 1
                        params = {"v": 1, "boot": boot, "seq": state["events"], "sentence_id": m["sentence_id"],
                                  "complete": True}
                        await ws.send(json.dumps({"session_id": sid, "type": "mcp", "payload": {
                            "jsonrpc": "2.0", "method": "notifications/weixue/played", "params": params}}))
                        log("played", m["sentence_id"])
                    if state["stops"] <= a.replies and reply:
                        state["reply_at"] = time.time() + a.reply_delay
        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        done, pending = await asyncio.wait(tasks, timeout=a.max_seconds, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        log("session ended:", "server closed" if tasks[1] in done else "timeout", "audio frames", state["audio"])

try:
    asyncio.run(main())
except websockets.ConnectionClosed as e:
    log("session closed by the server before hello:", e)
