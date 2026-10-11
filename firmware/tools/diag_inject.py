#!/usr/bin/env python3
"""Injects diag-build gestures and touches over the device's USB serial console while logging it.

One process owns the serial port (opened exclusively; serial_log.py must not run alongside, the
follow log replaces it). `follow` (or `--follow`) opens it, writes the redacted console to --log, runs
an optional script and serves other processes on a control socket; `send` hands lines to a
running `follow` through that socket, or opens the port itself with --direct when none runs.
Run under the device lock (scripts/device_lock.sh).

  diag_inject.py --port <port> follow --log <file> [--script <file>] [--seconds N] [--reset]
  diag_inject.py send 'diag gesture pick_up' 'diag swipe left'
  diag_inject.py --port <port> send --direct 'diag dump'

Device commands (diag build, one line each): `diag gesture pick_up|face_down|face_up|double_tap`,
`diag tap <x> <y>` (or `diag touch tap <x> <y>`), `diag swipe left|right|up|down`,
`diag press_boot`, `diag touch tour`, `diag dump`, `diag help`. The device answers each with
`DiagConsole: ok <line>: <result>` or `DiagConsole: error <line>: <reason>`.

Script lines (`#` starts a comment): `sleep <s>`; `send <line>` or a bare `diag ...` line;
`expect <regex> [timeout_s]` (waits for a console line after the previous send); `note <text>`.
`follow` exits 1 when a send or expect failed.
"""
import argparse
import os
import re
import shlex
import socket
import sys
import threading
import time

import serial

DEFAULT_CONTROL = os.path.join(
    os.environ.get("WEIXUE_VENDOR", os.path.expanduser("~/Projects/weixue-vendor")), ".diag-inject.sock")
ACK_RE = re.compile(r"DiagConsole: (ok|error) (.*?): (.*)$")

REDACTIONS = [
    (re.compile(r"(Found AP: )(.*?)(, BSSID)"), r"\1<ssid>\3"),
    (re.compile(r"(WiFi connecting to )(.*)"), r"\1<ssid>"),
    (re.compile(r"(connected with )(.*?)(, aid)"), r"\1<ssid>\3"),
    (re.compile(r"(Connected to WiFi: )(.*)"), r"\1<ssid>"),
    (re.compile(r"(token=)[^&\s\"]+"), r"\1<redacted>"),
    (re.compile(r"\S*private_\S*"), "<redacted>"),
    (re.compile(r"(Sec-WebSocket-Key:?\s*)\S+", re.I), r"\1<key>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b"), "<mac>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<IP>"),
]


def redact(line):
    for pattern, repl in REDACTIONS:
        line = pattern.sub(repl, line)
    return line


class DiagSerial:
    """Owns the port: a reader thread keeps every console line (redacted, host-timestamped) and
    writes it to the log; `send` writes a command line and waits for its DiagConsole answer."""

    def __init__(self, port, log_path, reset=False):
        self.port = port
        self.ser = self._open()
        self.log = open(log_path, "a", encoding="utf-8")
        self.log_lines = 0
        self.lines = []  # (monotonic, log line number, text)
        self.cond = threading.Condition()
        self.write_lock = threading.Lock()
        self.t0 = time.monotonic()
        self.closed = False
        if reset:
            self.ser.rts = True
            time.sleep(0.1)
            self.ser.rts = False
        self.reader = threading.Thread(target=self._reader, daemon=True)
        self.reader.start()

    def _open(self):
        s = serial.Serial()
        s.port = self.port
        s.baudrate = 115200
        s.timeout = 0.2
        s.write_timeout = 2
        s.dtr = False
        s.rts = False
        s.exclusive = True
        s.open()
        return s

    def _reader(self):
        buf = b""
        while not self.closed:
            try:
                buf += self.ser.read(4096)
            except (serial.SerialException, OSError):
                self._reopen()
                continue
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                text = redact(raw.decode("utf-8", "replace").rstrip("\r"))
                n = self._write(text)
                with self.cond:
                    self.lines.append((time.monotonic(), n, text))
                    self.cond.notify_all()

    def _reopen(self):
        try:
            self.ser.close()
        except (serial.SerialException, OSError):
            pass
        while not self.closed:
            try:
                self.ser = self._open()
                self._write("### port reopened")
                return
            except (serial.SerialException, OSError):
                time.sleep(0.2)

    def _write(self, text):
        with self.write_lock:
            stamp = time.strftime("%H:%M:%S", time.gmtime())
            self.log.write(f"[{time.monotonic() - self.t0:8.2f} {stamp}Z] {text}\n")
            self.log.flush()
            self.log_lines += 1
            return self.log_lines

    def note(self, text):
        print(text, flush=True)
        return self._write(f"### {text}")

    def mark(self):
        with self.cond:
            return len(self.lines)

    def wait_for(self, pattern, timeout, since=None):
        """First console line matching pattern at index >= since (default: now) within timeout;
        returns (log line number, text, match, monotonic time, index) or None."""
        rx = re.compile(pattern) if isinstance(pattern, str) else pattern
        end = time.monotonic() + timeout
        with self.cond:
            i = len(self.lines) if since is None else since
            while True:
                while i < len(self.lines):
                    t, n, text = self.lines[i]
                    i += 1
                    m = rx.search(text)
                    if m:
                        return n, text, m, t, i
                left = end - time.monotonic()
                if left <= 0:
                    return None
                self.cond.wait(left)

    def snapshot(self, since=0, until=None):
        """[(monotonic, log line number, text)] of the console lines at index since..until."""
        with self.cond:
            return list(self.lines[since:until])

    def find_all(self, pattern, since=0, until=None):
        rx = re.compile(pattern) if isinstance(pattern, str) else pattern
        with self.cond:
            chunk = self.lines[since:until]
        return [(n, text) for _, n, text in chunk if rx.search(text)]

    def send(self, line, timeout=3.0):
        """Writes one command line; returns (ok, result, log line number of the answer)."""
        since = self.mark()
        self._write(f"### send {line}")
        try:
            with self.write_lock:
                self.ser.write((line.strip() + "\n").encode())
        except (serial.SerialException, OSError) as e:
            return False, f"write failed: {e}", None
        want = line.strip()
        end = time.monotonic() + timeout
        while True:
            hit = self.wait_for(ACK_RE, max(0.0, end - time.monotonic()), since)
            if hit is None:
                return False, "no answer (not a diag build, or the console is not running)", None
            n, _, m, _, since = hit
            if m.group(2) == want:
                return m.group(1) == "ok", m.group(3), n

    def close(self):
        self.closed = True
        try:
            self.ser.close()
        except (serial.SerialException, OSError):
            pass
        self.log.close()


def serve_control(diag, path):
    """Accepts one line per connection on a Unix socket and answers `ok <result>` or `error ...`."""
    if os.path.exists(path):
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.connect(path)
            probe.close()
            raise SystemExit(f"another diag_inject.py follow serves {path}")
        except (ConnectionRefusedError, FileNotFoundError):
            os.unlink(path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    os.chmod(path, 0o600)
    srv.listen(4)

    def handle(conn):
        with conn:
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(256)
                if not chunk:
                    break
                data += chunk
            line = data.decode("utf-8", "replace").strip()
            if not line:
                return
            ok, result, _ = diag.send(line)
            conn.sendall(f"{'ok' if ok else 'error'} {result}\n".encode())

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return srv


def run_script(diag, path):
    failures = 0
    since = diag.mark()
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            word, _, rest = line.partition(" ")
            if word == "sleep":
                time.sleep(float(rest))
            elif word == "note":
                diag.note(rest)
            elif word in ("send", "diag"):
                cmd = rest if word == "send" else line
                since = diag.mark()
                ok, result, _ = diag.send(cmd)
                print(f"{'ok' if ok else 'ERROR'} {cmd}: {result}", flush=True)
                failures += not ok
            elif word == "expect":
                parts = shlex.split(rest)
                timeout = float(parts[1]) if len(parts) > 1 else 5.0
                hit = diag.wait_for(parts[0], timeout, since)
                if hit:
                    diag.note(f"expect {parts[0]!r}: line {hit[0]}")
                else:
                    diag.note(f"expect {parts[0]!r}: MISSING after {timeout} s")
                    failures += 1
            else:
                diag.note(f"unknown script line: {line}")
                failures += 1
    return failures


def control_send(path, line, timeout):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout + 2)
    s.connect(path)
    with s:
        s.sendall((line + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    return data.decode("utf-8", "replace").strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (follow, send --direct)")
    ap.add_argument("--control", default=DEFAULT_CONTROL, help="control socket path")
    sub = ap.add_subparsers(dest="cmd", required=True)
    fo = sub.add_parser("follow")
    fo.add_argument("--log", required=True)
    fo.add_argument("--script")
    fo.add_argument("--seconds", type=float, help="stop after this long (default: after the script, or never)")
    fo.add_argument("--reset", action="store_true", help="pulse RTS (hard reset) after opening")
    fo.add_argument("--no-control", action="store_true")
    se = sub.add_parser("send")
    se.add_argument("--direct", action="store_true", help="open the port instead of using a running follow")
    se.add_argument("--log", default=os.devnull)
    se.add_argument("--timeout", type=float, default=3.0)
    se.add_argument("lines", nargs="+")
    args = ap.parse_args(["follow" if a == "--follow" else a for a in sys.argv[1:]])

    if args.cmd == "send" and not args.direct:
        bad = 0
        for line in args.lines:
            try:
                answer = control_send(args.control, line, args.timeout)
            except (FileNotFoundError, ConnectionRefusedError):
                raise SystemExit(f"no follow on {args.control}; start one or use --direct --port")
            print(f"{line}: {answer}")
            bad += not answer.startswith("ok")
        return 1 if bad else 0
    if not args.port:
        raise SystemExit("--port is required")
    diag = DiagSerial(args.port, args.log, reset=getattr(args, "reset", False))
    try:
        if args.cmd == "send":
            bad = 0
            for line in args.lines:
                ok, result, _ = diag.send(line, args.timeout)
                print(f"{line}: {'ok' if ok else 'error'} {result}")
                bad += not ok
            return 1 if bad else 0
        srv = None if args.no_control else serve_control(diag, args.control)
        end = time.monotonic() + args.seconds if args.seconds else None
        failures = 0
        try:
            if args.script:
                failures = run_script(diag, args.script)
                if end is None:
                    return 1 if failures else 0
            while end is None or time.monotonic() < end:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            if srv is not None:
                srv.close()
                if os.path.exists(args.control):
                    os.unlink(args.control)
        return 1 if failures else 0
    finally:
        diag.close()


if __name__ == "__main__":
    sys.exit(main())
