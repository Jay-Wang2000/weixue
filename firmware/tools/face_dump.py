#!/usr/bin/env python3
"""Decodes FACEDUMP screen dumps from a serial log (diag firmware) into PNG files."""
import argparse
import base64
import struct
import sys
import zlib
from pathlib import Path


def png(path: Path, w: int, h: int, rgb: bytes) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    rows = b"".join(b"\x00" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def decode(w: int, h: int, data: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(data) - 3, 4):
        count, c = struct.unpack_from("<HH", data, i)
        r, g, b = (c >> 11) & 0x1F, (c >> 5) & 0x3F, c & 0x1F
        out += bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))) * count
    if len(out) != w * h * 3:
        raise ValueError(f"decoded {len(out) // 3} pixels, expected {w * h}")
    return bytes(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("log")
    ap.add_argument("out_dir")
    args = ap.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dumps = {}
    for line in Path(args.log).read_text(errors="replace").splitlines():
        at = line.find("FACEDUMP ")
        if at < 0:
            continue
        parts = line[at:].split()
        if len(parts) < 3:
            continue
        key = parts[1]
        if parts[2] == "begin" and len(parts) >= 7:
            dumps[key] = {"w": int(parts[3]), "h": int(parts[4]), "label": f"{parts[5]}-{parts[6]}", "chunks": {}}
        elif parts[2] == "end" and key in dumps:
            d = dumps.pop(key)
            data = b"".join(base64.b64decode(d["chunks"][i]) for i in sorted(d["chunks"]))
            path = out_dir / f"face-{int(key):03d}-{d['label']}.png"
            png(path, d["w"], d["h"], decode(d["w"], d["h"], data))
            print(path)
        elif key in dumps and len(parts) >= 4 and parts[2].isdigit():
            dumps[key]["chunks"][int(parts[2])] = parts[3]
    return 0


if __name__ == "__main__":
    sys.exit(main())
