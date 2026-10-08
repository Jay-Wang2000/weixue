#!/usr/bin/env python3
"""Simulated agent device (docs/agent-protocol.md): the firmware's agent side without hardware.
Long-polls with X-Weixue-Agent-Seq, applies `agent` / `agent_sync` items by seq per task (the
weixue_agent_store rules), acks `shown` in session and implicitly through the poll, opens a
session on `speak:true` (hello with features.agent), sends `played` complete when the audio of an
`ack:true` sentence has played out (60 ms per frame from its first frame) or cut with
--cut-after-ms, and answers `debug_reply` after 2 s (loopback) over its session or a short
session of its own. --busy N first holds a user session open for N seconds.

  agent_sim_device.py --base http://<host>:18003 --ws ws://<host>:18000/xiaozhi/v1/ --device <mac> --seconds 120"""
import argparse
import asyncio
import json
import random
import time
import uuid

import aiohttp
import websockets

FRAME_MS = 60
DROP_BYTES = 2048
LOOPBACK_S = 2.0


def log(*a):
    print(f"{time.time():.3f} {time.strftime('%H:%M:%S')}", *a, flush=True)


class SimDevice:
    def __init__(self, args):
        self.a = args
        self.boot = random.randrange(1, 2 ** 32)
        self.seq = 0
        self.tasks = {}
        self.max_seq = 0
        self.client_id = str(uuid.uuid4())
        self.ident = {"Device-Id": args.device, "Client-Id": self.client_id}
        self.ws = None
        self.session_id = None
        self.pending_out = []
        self.state = "idle"

    def next_seq(self):
        self.seq += 1
        return self.seq

    # Store

    def apply(self, payload, via):
        size = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
        if size > DROP_BYTES:
            log(f"agent: dropped frame of {size} bytes (limit {DROP_BYTES}) via={via}")
            return
        kind, v, seq = payload.get("kind"), payload.get("v"), payload.get("seq")
        if v != 1 or not isinstance(seq, int) or seq < 1:
            log(f"agent: dropped frame (bad v or seq) via={via}")
            return
        if kind == "agent_sync":
            keep = set(payload.get("task_ids") or [])
            dropped = [t for t in self.tasks if t not in keep]
            for t in dropped:
                del self.tasks[t]
            self.max_seq = max(self.max_seq, seq)
            log(f"agent: sync seq={seq} listed={len(keep)} dropped={len(dropped)} tasks={len(self.tasks)} via={via}")
            return
        if kind != "agent":
            log(f"agent: dropped frame (unknown kind) via={via}")
            return
        task = payload.get("task_id")
        old = self.tasks.get(task)
        if old is not None and seq == old["seq"]:
            result = "duplicate"
        elif old is not None and seq < old["seq"]:
            result = "stale"
        else:
            result = "applied"
            self.tasks[task] = payload
            self.max_seq = max(self.max_seq, seq)
        log(f"agent: chat={payload.get('chat_id')} task={task} state={payload.get('state')} seq={seq} "
            f"tier={payload.get('delivery')} speaks={int(bool(payload.get('speaks')))} result={result} via={via}")
        debug = payload.get("debug_reply")
        if result == "applied" and debug and self.a.loopback:
            response = {"kind": debug["kind"], "chat_id": payload.get("chat_id"), "task_id": task, "via": "loopback"}
            if debug["kind"] in ("approve", "deny", "answer"):
                response["card_id"] = payload.get("card_id")
            for k in ("option_id", "text"):
                if debug.get(k):
                    response[k] = debug[k]
            asyncio.get_running_loop().call_later(LOOPBACK_S, lambda: self.send_event("agent_response", response))

    # Device → server

    def notification(self, method, params):
        return {"jsonrpc": "2.0", "method": f"notifications/weixue/{method}",
                "params": {"v": 1, "boot": self.boot, "seq": self.next_seq(), **params}}

    def send_event(self, method, params):
        payload = self.notification(method, params)
        log(f"agent {method}: {json.dumps(payload['params'], ensure_ascii=False)}")
        if self.ws is not None and self.session_id:
            asyncio.get_running_loop().create_task(self._send_mcp(self.ws, self.session_id, payload))
        else:
            asyncio.get_running_loop().create_task(self._event_session(payload))

    async def _send_mcp(self, ws, session_id, payload):
        try:
            await ws.send(json.dumps({"session_id": session_id, "type": "mcp", "payload": payload}))
        except websockets.ConnectionClosed:
            log("event not sent: session closed")

    async def _event_session(self, payload):
        """Patch 26: a short session of its own, hello without features.agent."""
        async with websockets.connect(self.a.ws, additional_headers={**self.ident, "Protocol-Version": "1"}) as ws:
            await ws.send(json.dumps({"type": "hello", "version": 1, "features": {"mcp": True}, "transport": "websocket",
                                      "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1,
                                                       "frame_duration": 60}}))
            hello = json.loads(await ws.recv())
            await self._send_mcp(ws, hello.get("session_id"), payload)
            await asyncio.sleep(0.5)
        log("device event sent (own session)")

    # Session

    async def session(self, invoke, hold=None):
        headers = {**self.ident, "Protocol-Version": "1"}
        async with websockets.connect(self.a.ws, additional_headers=headers) as ws:
            await ws.send(json.dumps({"type": "hello", "version": 1, "features": {"mcp": True, "agent": 1},
                                      "transport": "websocket",
                                      "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1,
                                                       "frame_duration": 60}}))
            hello = json.loads(await ws.recv())
            self.ws, self.session_id = ws, hello.get("session_id")
            self.state = "connecting"
            log(f"session opened ({'proactive' if invoke else 'user'}) id={self.session_id}")
            if invoke:
                await ws.send(json.dumps({"session_id": self.session_id, "type": "listen", "state": "detect",
                                          "text": "weixue:proactive"}))
            await ws.send(json.dumps({"session_id": self.session_id, "type": "listen", "state": "start", "mode": "auto"}))
            self.state = "listening"
            track = {"sentence": None, "first": None, "frames": 0, "stopped": False, "cut": False}
            shown = {"handle": None}
            deadline = time.time() + hold if hold else None

            def queue_shown():
                if shown["handle"] is None:
                    def fire():
                        shown["handle"] = None
                        self.send_event("agent_response", {"kind": "shown", "ack_upto": self.max_seq, "via": "tap"})
                    shown["handle"] = asyncio.get_running_loop().call_later(0.2, fire)

            async def played_watch():
                while True:
                    await asyncio.sleep(0.1)
                    sid = track["sentence"]
                    if sid is None or track["first"] is None:
                        continue
                    played_ms = int((time.time() - track["first"]) * 1000)
                    audio_ms = track["frames"] * FRAME_MS
                    if self.a.cut_after_ms and not track["cut"] and played_ms >= self.a.cut_after_ms:
                        track["cut"] = True
                        await ws.send(json.dumps({"session_id": self.session_id, "type": "abort", "reason": "wake_word_detected"}))
                        self.send_event("played", {"sentence_id": sid, "complete": False,
                                                   "cut_at_ms": min(played_ms, audio_ms), "reason": "wake"})
                        track.update(sentence=None, first=None, frames=0, stopped=False)
                    elif track["stopped"] and played_ms >= audio_ms:
                        log(f"agent played: sentence={sid} complete=1 played_ms={audio_ms}")
                        self.send_event("played", {"sentence_id": sid, "complete": True})
                        track.update(sentence=None, first=None, frames=0, stopped=False)

            watcher = asyncio.create_task(played_watch())
            try:
                while True:
                    timeout = None if deadline is None else max(0.0, deadline - time.time())
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout)
                    except asyncio.TimeoutError:
                        log("user session ends")
                        break
                    if isinstance(msg, bytes):
                        if track["sentence"] is not None:
                            track["frames"] += 1
                            if track["first"] is None:
                                track["first"] = time.time()
                        continue
                    m = json.loads(msg)
                    if m.get("type") == "custom" and isinstance(m.get("payload"), dict):
                        self.apply(m["payload"], "push")
                        queue_shown()
                    elif m.get("type") == "tts":
                        st = m.get("state")
                        log("<<", json.dumps({k: v for k, v in m.items() if k != "session_id"}, ensure_ascii=False))
                        if st == "start":
                            self.state = "speaking"
                            if track["sentence"] and track["sentence"] != m.get("sentence_id"):
                                self.send_event("played", {"sentence_id": track["sentence"], "complete": False,
                                                           "cut_at_ms": track["frames"] * FRAME_MS, "reason": "superseded"})
                                track.update(sentence=None, first=None, frames=0, stopped=False)
                            if m.get("ack") and m.get("sentence_id"):
                                track.update(sentence=m["sentence_id"], first=None, frames=0, stopped=False, cut=False)
                                log(f"agent: tracking playback of sentence {m['sentence_id']}")
                        elif st == "stop":
                            self.state = "listening"
                            if track["sentence"] and m.get("sentence_id") in (None, track["sentence"]):
                                track["stopped"] = True
            except websockets.ConnectionClosed:
                pass
            finally:
                watcher.cancel()
                if track["sentence"] and not track["stopped"]:
                    self.send_event("played", {"sentence_id": track["sentence"], "complete": False,
                                               "cut_at_ms": track["frames"] * FRAME_MS, "reason": "closed"})
                self.ws, self.session_id = None, None
        self.state = "idle"
        log("state: idle (session closed)")

    async def poll_loop(self, until):
        async with aiohttp.ClientSession() as http:
            while time.time() < until:
                headers = {**self.ident, "X-Weixue-Agent-Seq": str(self.max_seq)}
                try:
                    async with http.get(self.a.base + "/weixue/proactive/poll", headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=40)) as r:
                        status = r.status
                        body = await r.json() if status == 200 else None
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    log("poll failed:", type(e).__name__)
                    await asyncio.sleep(5)
                    continue
                if status == 200:
                    items = body.get("agent") or []
                    log(f"poll 200 speak={body.get('speak')} urgent={body.get('urgent')} items={len(items)} header={headers['X-Weixue-Agent-Seq']}")
                    for p in items:
                        self.apply(p, "poll")
                    if body.get("speak", True):
                        await self.session(invoke=True)
                    else:
                        await asyncio.sleep(0.2)
                elif status == 204:
                    await asyncio.sleep(1)
                else:
                    log("poll", status)
                    await asyncio.sleep(5)


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True)
    ap.add_argument("--ws", required=True)
    ap.add_argument("--device", required=True)
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--busy", type=float, default=0, help="hold a user session open this many seconds first")
    ap.add_argument("--cut-after-ms", type=int, default=0, help="abort ack sentences after this much playback")
    ap.add_argument("--no-loopback", dest="loopback", action="store_false")
    a = ap.parse_args()
    dev = SimDevice(a)
    log(f"agent sim device boot={dev.boot} device={a.device}")
    until = time.time() + a.seconds
    if a.busy:
        await dev.session(invoke=False, hold=a.busy)
    await dev.poll_loop(until)
    log(f"done: tasks={sorted(dev.tasks)} max_seq={dev.max_seq}")


if __name__ == "__main__":
    asyncio.run(main())
