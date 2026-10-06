#!/usr/bin/env python3
"""On-device IMU gesture check over the serial console.

Counts the firmware's `IMU gesture: <name>` lines. `idle` listens for a fixed time; `trials`
prompts for each attempt of one gesture and waits for its detection. The redacted serial
stream and a summary are written to --log.
"""
import argparse
import queue
import re
import sys
import threading
import time

import serial

GESTURE_RE = re.compile(r"IMU gesture: (\w+)")
TAP_RE = re.compile(r"IMU tap: status1=0x[0-9A-F]+ tap_status=0x([0-9A-F]+)")
SSID_RES = [
    re.compile(r"(Found AP: )(.*?)(, BSSID)"),
    re.compile(r"(WiFi connecting to )(.*)()"),
    re.compile(r"(connected with )(.*?)(, aid)"),
    re.compile(r"(Connected to WiFi: )(.*)()"),
]
BSSID_RES = [
    re.compile(r"(BSSID: )([0-9a-fA-F:]{17})"),
    re.compile(r"(bssid = )([0-9a-fA-F:]{17})"),
    re.compile(r"(\(ifx:\d+, )([0-9a-fA-F:]{17})"),
]
SECRET_RES = [re.compile(r"(token=)[^&\s\"]+"), re.compile(r"\S*private_\S*")]

TRIALS = {
    "pick_up": ("Pick the device up from the table, then put it back down and leave it still.",
                ["pick_up"]),
    "face_down": ("Turn the device face-down on the table, wait for 'Muted', then turn it face-up.",
                  ["face_down", "face_up"]),
    "double_tap": ("Double-tap the device (two quick taps).", ["double_tap"]),
}


def redact(line):
    for r in SSID_RES:
        line = r.sub(r"\1<ssid>\3", line)
    for r in BSSID_RES:
        line = r.sub(r"\1<bssid>", line)
    line = SECRET_RES[0].sub(r"\1<redacted>", line)
    line = SECRET_RES[1].sub("<redacted>", line)
    return line


class Console:
    def __init__(self, port, log_path):
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = 115200
        self.ser.timeout = 0.2
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.open()
        self.log = open(log_path, "a", encoding="utf-8")
        self.lines = queue.Queue()
        self.t0 = time.monotonic()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        buf = b""
        while True:
            buf += self.ser.read(4096)
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = redact(raw.decode("utf-8", "replace").rstrip("\r"))
                self.write(line)
                self.lines.put((time.monotonic(), line))

    def write(self, line):
        self.log.write(f"[{time.monotonic() - self.t0:8.2f}] {line}\n")
        self.log.flush()

    def note(self, text):
        print(text, flush=True)
        self.write(f"### {text}")

    def wait_boot(self, seconds=30):
        """Opening the port resets the board; waits for its first idle state."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                _, line = self.lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if "-> idle" in line:
                self.note("device idle")
                return
        self.note(f"no idle state within {seconds} s")

    def drain(self):
        while not self.lines.empty():
            self.lines.get_nowait()

    def collect(self, seconds, until=None):
        """Returns (gestures, taps) seen within seconds; stops early once every name in until was seen."""
        end = time.monotonic() + seconds
        gestures, taps = [], []
        while time.monotonic() < end:
            try:
                _, line = self.lines.get(timeout=0.2)
            except queue.Empty:
                continue
            m = GESTURE_RE.search(line)
            if m:
                gestures.append(m.group(1))
                print(f"  detected: {m.group(1)}", flush=True)
            m = TAP_RE.search(line)
            if m:
                taps.append(int(m.group(1), 16) & 0x03)
            if until and all(name in gestures for name in until):
                break
        return gestures, taps


def run_idle(con, seconds):
    con.note(f"idle: leave the device untouched for {seconds} s")
    gestures, taps = con.collect(seconds)
    con.note(f"idle summary: {seconds} s, gestures={gestures or 'none'}, "
             f"tap_engine_events={len(taps)}")
    return len(gestures) == 0


def run_trials(con, gesture, count, timeout):
    prompt, expect = TRIALS[gesture]
    hits, extras = 0, []
    con.note(f"trials: {gesture} x{count}")
    for i in range(1, count + 1):
        input(f"[{i}/{count}] Press Enter, then: {prompt} ")
        con.drain()
        con.write(f"### trial {i}/{count} start")
        gestures, taps = con.collect(timeout, until=expect)
        ok = all(name in gestures for name in expect)
        hits += ok
        extras += [g for g in gestures if g not in expect]
        con.note(f"trial {i}/{count}: {'detected' if ok else 'MISSED'} gestures={gestures} "
                 f"tap_counts={taps}")
        time.sleep(2)
    con.note(f"{gesture} summary: {hits}/{count} detected, other gestures={extras or 'none'}")
    return hits


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", required=True)
    ap.add_argument("--log", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    idle = sub.add_parser("idle")
    idle.add_argument("--seconds", type=int, default=300)
    trials = sub.add_parser("trials")
    trials.add_argument("gesture", choices=sorted(TRIALS))
    trials.add_argument("--count", type=int, default=10)
    trials.add_argument("--timeout", type=float, default=10.0)
    args = ap.parse_args()

    con = Console(args.port, args.log)
    con.wait_boot()
    if args.cmd == "idle":
        sys.exit(0 if run_idle(con, args.seconds) else 1)
    hits = run_trials(con, args.gesture, args.count, args.timeout)
    sys.exit(0 if hits * 10 >= args.count * 9 else 1)


if __name__ == "__main__":
    main()
