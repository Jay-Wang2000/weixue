#!/usr/bin/env python3
"""Simulated agent device (docs/agent-protocol.md): the firmware's agent side without hardware.
Long-polls with X-Weixue-Agent-Seq, applies `agent` / `agent_sync` items by seq per task (the
weixue_agent_store rules), acks `shown` in session and implicitly through the poll, opens a
session on `speak:true` (hello with features.agent), sends `played` complete when the audio of an
`ack:true` sentence has played out (60 ms per frame from its first frame) or cut with
--cut-after-ms, and answers `debug_reply` after 2 s (loopback) over its session or a short
session of its own. --busy N first holds a user session open for N seconds.

Responses other than `shown` stay in the outbox until their `agent_ack` receipt arrives (2 s
wait, resends of the same seq after 1, 3 and 9 s; --no-receipts sends once, as v1 firmware).
--weak-link B delivers only the first B bytes of a poll response larger than B and then resets
the connection (1440 = one TCP segment of the device); a 200 body that did
not arrive whole is a failed poll, polled again after 1 s (--old-firmware: treated as pending,
so a session opens with the bare invocation). --invoke-at S opens one session with the bare
invocation S seconds after start (a proactive session without a current lease). --ws-delay S
delays every server → device WebSocket byte by S seconds and --ws-up-delay every device → server
byte from 0.5 s after the connection opened (a local relay; the handshake and hello go through at
once), so the server's close handshake overlaps frames the device is still sending.

  agent_sim_device.py --base http://<host>:18003 --ws ws://<host>:18000/xiaozhi/v1/ --device <mac> --seconds 120"""
import argparse
import asyncio
import json
import random
import time
import uuid
from urllib.parse import urlsplit

import websockets

FRAME_MS = 60
DROP_BYTES = 2048
LOOPBACK_S = 2.0
RECEIPT_WAIT_S = 2.0
RETRY_S = (1, 3, 9)
POLL_TIMEOUT_S = 40


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
        self.awaiting = {}
        self.ws_url = args.ws
        self.stats = {"bodies": 0, "unreadable": 0, "max_body": 0, "receipts": 0, "dropped": 0}

    async def start_relay(self):
        """Local relay for --ws-delay / --ws-up-delay: each direction's bytes arrive that late, in
        order; a closed side closes the other once its bytes are through."""
        url = urlsplit(self.a.ws)

        async def delayed(reader, writer, lag, after=0.0):
            queue = asyncio.Queue()
            opened = time.time()

            async def read():
                try:
                    while True:
                        data = await reader.read(65536)
                        await queue.put((time.time() + (lag if time.time() - opened >= after else 0), data))
                        if not data:
                            return
                except (ConnectionError, OSError):
                    await queue.put((time.time() + lag, b""))

            rt = asyncio.get_running_loop().create_task(read())
            try:
                while True:
                    due, data = await queue.get()
                    await asyncio.sleep(max(0.0, due - time.time()))
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                rt.cancel()
                writer.close()

        async def handle(dev_reader, dev_writer):
            up_reader, up_writer = await asyncio.open_connection(url.hostname, url.port)
            loop = asyncio.get_running_loop()
            loop.create_task(delayed(dev_reader, up_writer, self.a.ws_up_delay, after=0.5))
            loop.create_task(delayed(up_reader, dev_writer, self.a.ws_delay))

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        self.ws_url = f"ws://127.0.0.1:{port}{url.path}"
        log(f"ws relay: server -> device {self.a.ws_delay} s, device -> server {self.a.ws_up_delay} s, port {port}")

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
        if kind == "agent_ack":
            self.on_receipt(payload)
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
        if self.a.receipts and method == "agent_response" and params.get("kind") != "shown":
            seq = payload["params"]["seq"]
            self.awaiting[seq] = asyncio.Event()
            asyncio.get_running_loop().create_task(self._deliver(payload, seq))
        else:
            asyncio.get_running_loop().create_task(self._transmit(payload, receipt=False))

    async def _transmit(self, payload, receipt):
        if self.ws is not None and self.session_id:
            await self._send_mcp(self.ws, self.session_id, payload)
        else:
            await self._event_session(payload, receipt)

    async def _deliver(self, payload, seq):
        """Outbox entry of a response: sent until its receipt arrives, at most 4 attempts."""
        label = f"{payload['params']['kind']} seq {seq}"
        for attempt in range(1, len(RETRY_S) + 2):
            await self._transmit(payload, receipt=True)
            try:
                await asyncio.wait_for(self.awaiting[seq].wait(), RECEIPT_WAIT_S)
                log(f"agent ack received: {label} (attempt {attempt})")
                del self.awaiting[seq]
                return
            except asyncio.TimeoutError:
                pass
            if attempt > len(RETRY_S):
                break
            log(f"agent ack without receipt (attempt {attempt}), resend in {RETRY_S[attempt - 1] * 1000} ms: {label}")
            await asyncio.sleep(RETRY_S[attempt - 1])
            if self.awaiting[seq].is_set():
                log(f"agent ack received: {label} (late)")
                del self.awaiting[seq]
                return
        self.stats["dropped"] += 1
        log(f"agent ack dropped after {attempt} attempts: {label}")
        del self.awaiting[seq]

    def on_receipt(self, p):
        self.stats["receipts"] += 1
        known = p.get("boot") == self.boot and p.get("seq") in self.awaiting
        log(f"agent receipt: {p.get('response')} seq={p.get('seq')} card={p.get('card_id')} result={p.get('result')}"
            + ("" if known else " (no response waits for it)"))
        if known:
            self.awaiting[p["seq"]].set()

    async def _send_mcp(self, ws, session_id, payload):
        try:
            await ws.send(json.dumps({"session_id": session_id, "type": "mcp", "payload": payload}))
        except websockets.ConnectionClosed:
            log("event not sent: session closed")

    async def _event_session(self, payload, receipt=False):
        """Patch 26: a short session of its own, hello without features.agent; it lingers 0.5 s, or
        until the receipt arrives (at most RECEIPT_WAIT_S)."""
        async with websockets.connect(self.ws_url, additional_headers={**self.ident, "Protocol-Version": "1"}, open_timeout=60) as ws:
            await ws.send(json.dumps({"type": "hello", "version": 1, "features": {"mcp": True}, "transport": "websocket",
                                      "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1,
                                                       "frame_duration": 60}}))
            hello = json.loads(await ws.recv())
            await self._send_mcp(ws, hello.get("session_id"), payload)
            deadline = time.time() + (RECEIPT_WAIT_S if receipt else 0.5)
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), deadline - time.time())
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    break
                m = json.loads(msg) if isinstance(msg, str) else {}
                if m.get("type") == "custom" and isinstance(m.get("payload"), dict):
                    self.apply(m["payload"], "own")
                    if receipt and m["payload"].get("kind") == "agent_ack":
                        break
        log("device event sent (own session)")

    # Session

    async def session(self, invoke, hold=None):
        headers = {**self.ident, "Protocol-Version": "1"}
        async with websockets.connect(self.ws_url, additional_headers=headers, open_timeout=60, close_timeout=30) as ws:
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
                        if m["payload"].get("kind") != "agent_ack":
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

    async def http_get(self, path, headers):
        """GET with Connection: close, read like the firmware HttpClient: (status, Content-Length
        or None, body bytes, cut). With --weak-link a response larger than that many bytes ends
        after them (cut=True)."""
        url = urlsplit(self.a.base)
        reader, writer = await asyncio.open_connection(url.hostname, url.port or 80)
        lines = [f"GET {path} HTTP/1.1", f"Host: {url.netloc}", *(f"{k}: {v}" for k, v in headers.items()),
                 "Connection: close", "", ""]
        writer.write("\r\n".join(lines).encode())
        await writer.drain()
        data, cut = b"", False
        try:
            while True:
                chunk = await asyncio.wait_for(reader.read(65536), POLL_TIMEOUT_S)
                if not chunk:
                    break
                data += chunk
                if self.a.weak_link and len(data) > self.a.weak_link:
                    data, cut = data[:self.a.weak_link], True
                    break
        finally:
            writer.close()
        head, _, body = data.partition(b"\r\n\r\n")
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        status = int(status_line.split()[1]) if status_line.startswith("HTTP/") else 0
        length = None
        for line in header_lines:
            key, _, value = line.partition(":")
            if key.strip().lower() == "content-length":
                length = int(value.strip())
        return status, length, body, cut, len(head) + 4 + (length or 0)

    async def poll_loop(self, until):
        while time.time() < until:
            headers = {**self.ident, "X-Weixue-Agent-Seq": str(self.max_seq)}
            try:
                status, length, raw, cut, total = await self.http_get("/weixue/proactive/poll", headers)
            except (OSError, asyncio.TimeoutError, ValueError, IndexError) as e:
                log("poll failed:", type(e).__name__)
                await asyncio.sleep(5)
                continue
            if status == 200:
                self.stats["bodies"] += 1
                self.stats["max_body"] = max(self.stats["max_body"], total)
                body = None
                if length is None or len(raw) == length:
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        body = None
                if not isinstance(body, dict):
                    self.stats["unreadable"] += 1
                    log(f"poll 200 body unreadable ({len(raw)} of {length} bytes, response {total} B"
                        f"{', connection reset after ' + str(self.a.weak_link) + ' B' if cut else ''}) "
                        f"header={headers['X-Weixue-Agent-Seq']}")
                    if self.a.old_firmware:
                        log("Proactive message pending (urgent=0)")
                        await self.session(invoke=True)
                    else:
                        await asyncio.sleep(1)
                    continue
                items = body.get("agent") or []
                log(f"poll 200 speak={body.get('speak')} urgent={body.get('urgent')} items={len(items)} "
                    f"bytes={total} header={headers['X-Weixue-Agent-Seq']}")
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
    ap.add_argument("--no-receipts", dest="receipts", action="store_false", help="send responses once (v1 firmware)")
    ap.add_argument("--weak-link", type=int, default=0, help="poll responses larger than this many bytes are cut there")
    ap.add_argument("--old-firmware", action="store_true", help="an unreadable 200 poll body opens a session")
    ap.add_argument("--invoke-at", type=float, default=0, help="open one bare-invocation session this many seconds in")
    ap.add_argument("--ws-delay", type=float, default=0, help="delay server -> device WebSocket bytes this many seconds")
    ap.add_argument("--ws-up-delay", type=float, default=0, help="delay device -> server WebSocket bytes this many seconds")
    a = ap.parse_args()
    dev = SimDevice(a)
    if a.ws_delay or a.ws_up_delay:
        await dev.start_relay()
    log(f"agent sim device boot={dev.boot} device={a.device} receipts={a.receipts} weak_link={a.weak_link} "
        f"old_firmware={a.old_firmware}")
    until = time.time() + a.seconds
    if a.busy:
        await dev.session(invoke=False, hold=a.busy)
    poller = asyncio.create_task(dev.poll_loop(until))
    if a.invoke_at:
        await asyncio.sleep(a.invoke_at)
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)
        log("invoking without a current lease")
        await dev.session(invoke=True)
        poller = asyncio.create_task(dev.poll_loop(until))
    await poller
    await asyncio.sleep(1)
    log(f"done: tasks={sorted(dev.tasks)} max_seq={dev.max_seq} stats={dev.stats} awaiting={sorted(dev.awaiting)}")


if __name__ == "__main__":
    asyncio.run(main())
