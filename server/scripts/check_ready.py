#!/usr/bin/env python3
"""Wait until the server answers on OTA and WebSocket.

Usage: check_ready.py HOST [--timeout S] [--ota-port N] [--ws-port N]

Ready means: GET /xiaozhi/ota/ returns 200 with the upstream OTA banner, and a WebSocket
handshake on /xiaozhi/v1/ without device headers returns 101 followed by a text frame.
Exits 0 when both hold before the timeout (default 90 s), else 1.
"""
import argparse
import base64
import os
import socket
import sys
import time
import urllib.request

OTA_BANNER = "OTA接口运行正常"


def ota_ready(host, port):
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/xiaozhi/ota/", timeout=3) as r:
            return r.status == 200 and OTA_BANNER in r.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return False


def _recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


def ws_ready(host, port):
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET /xiaozhi/v1/ HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    try:
        with socket.create_connection((host, port), timeout=3) as sock:
            sock.sendall(request.encode())
            head = b""
            while b"\r\n\r\n" not in head:
                head += _recv_exact(sock, 1)
            if not head.startswith(b"HTTP/1.1 101"):
                return False
            first, length = _recv_exact(sock, 2)
            return first & 0x0F == 0x1 and length & 0x7F > 0
    except (OSError, ConnectionError, ValueError):
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="Wait for OTA and WebSocket readiness.")
    parser.add_argument("host")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--ota-port", type=int, default=8003)
    parser.add_argument("--ws-port", type=int, default=8000)
    args = parser.parse_args(argv)
    deadline = time.monotonic() + args.timeout
    ota = ws = False
    while True:
        ota = ota or ota_ready(args.host, args.ota_port)
        ws = ws or ws_ready(args.host, args.ws_port)
        if ota and ws:
            print(f"ready: OTA :{args.ota_port}, WebSocket :{args.ws_port}")
            return 0
        if time.monotonic() >= deadline:
            print(f"not ready after {args.timeout:.0f}s: OTA {'ok' if ota else 'down'}, WebSocket {'ok' if ws else 'down'}")
            return 1
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
