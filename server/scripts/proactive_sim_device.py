"""Simulated device for the proactive flow (docs/proactive.md): long-polls, opens a session on 200,
streams silence and optionally a 16 kHz mono WAV reply after the first `tts stop` (with
--reply-during-playback also after the first audio frame), and prints the server's messages.
--direct opens a session without polling, as a user-started session; --wake TEXT then sends a
wake-word `listen detect` with TEXT. Requests carry Device-Id
and Client-Id, plus Authorization when --token is given."""
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
    a = ap.parse_args()
    ident = {"Device-Id": a.device, "Client-Id": a.client_id}
    if a.token:
        ident["Authorization"] = f"Bearer {a.token}"
    async with aiohttp.ClientSession() as http:
        polls = 0
        while not a.direct:
            async with http.get(a.base + "/weixue/proactive/poll", headers=ident) as r:
                body = await r.text()
                log("poll", r.status, body.strip())
                polls += 1
                if r.status == 200:
                    break
                if a.polls and polls >= a.polls:
                    return
                if r.status not in (200, 204):
                    await asyncio.sleep(1)
    enc = opuslib_next.Encoder(16000, 1, "voip")
    silence = enc.encode(b"\0" * FRAME * 2, FRAME)
    reply = [enc.encode(f, FRAME) for f in frames_from_wav(a.reply)] if a.reply else []
    headers = {**ident, "Protocol-Version": "1"}
    async with websockets.connect(a.ws, additional_headers=headers) as ws:
        await ws.send(json.dumps({"type": "hello", "version": 1, "features": {"mcp": True}, "transport": "websocket",
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
            await ws.send(json.dumps({"session_id": sid, "type": "listen", "state": "detect", "text": "weixue:proactive"}))
        elif a.wake:
            await ws.send(json.dumps({"session_id": sid, "type": "listen", "state": "detect", "text": a.wake}))
        await ws.send(json.dumps({"session_id": sid, "type": "listen", "state": "start", "mode": "realtime"}))
        state = {"stops": 0, "reply_at": None, "audio": 0}
        async def sender():
            queue = []
            while not ws.close_code:
                if state["reply_at"] and time.time() >= state["reply_at"] and reply:
                    queue, state["reply_at"] = list(reply), None
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
                    continue
                m = json.loads(msg)
                if m.get("type") == "mcp":
                    continue
                log("<<", json.dumps({k: v for k, v in m.items() if k != "session_id"}, ensure_ascii=False), f"(audio frames so far {state['audio']})")
                if m.get("type") == "tts" and m.get("state") == "stop":
                    state["stops"] += 1
                    if state["stops"] == 1 and reply:
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
