#!/usr/bin/env python3
"""Captures the device serial console to a file for a fixed time, optionally after a hard reset.

Uses the project .venv pyserial. Reopens the port when USB re-enumerates after a reset.
"""
import argparse
import sys
import time

import serial


def open_port(port: str) -> serial.Serial:
    s = serial.Serial()
    s.port = port
    s.baudrate = 115200
    s.timeout = 0.2
    s.dtr = False
    s.rts = False
    s.open()
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", required=True)
    ap.add_argument("--seconds", type=float, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--reset", action="store_true", help="pulse RTS (USB-Serial-JTAG hard reset) first")
    args = ap.parse_args()

    deadline = time.monotonic() + args.seconds
    s = open_port(args.port)
    if args.reset:
        s.rts = True
        time.sleep(0.1)
        s.rts = False
    with open(args.out, "wb") as out:
        while time.monotonic() < deadline:
            try:
                data = s.read(4096)
            except (serial.SerialException, OSError):
                s.close()
                while time.monotonic() < deadline:
                    try:
                        s = open_port(args.port)
                        break
                    except (serial.SerialException, OSError):
                        time.sleep(0.2)
                continue
            if data:
                out.write(data)
                out.flush()
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
