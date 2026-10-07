#!/usr/bin/env python3
"""Prints the sha256 of each written NVS string board:uuid in a flash image or NVS partition dump, one per line.

Uses ESP-IDF's nvs_parser from $IDF_PATH (default ~/Projects/weixue-vendor/esp-idf).
"""
import argparse
import hashlib
import os
import sys

NVS_OFFSET = 0x3B000
NVS_SIZE = 0xD2000


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file")
    ap.add_argument("--offset", type=lambda v: int(v, 0), default=NVS_OFFSET,
                    help="NVS partition offset in the file (0 for a partition dump)")
    args = ap.parse_args()

    idf = os.environ.get("IDF_PATH") or os.path.expanduser("~/Projects/weixue-vendor/esp-idf")
    sys.path.insert(0, os.path.join(idf, "components/nvs_flash/nvs_partition_tool"))
    from nvs_parser import NVS_Partition  # noqa: E402

    with open(args.file, "rb") as f:
        f.seek(args.offset)
        raw = f.read(NVS_SIZE)
    if len(raw) != NVS_SIZE:
        print(f"error: {args.file} has no {NVS_SIZE}-byte NVS partition at {args.offset:#x}", file=sys.stderr)
        return 1
    part = NVS_Partition("nvs", bytearray(raw))
    entries = [e for p in part.pages for e in p.entries if e.state == "Written"]
    ns = {e.data["value"]: e.key for e in entries if e.metadata["namespace"] == 0}
    found = set()
    for e in entries:
        if (e.metadata["namespace"] != 0 and ns.get(e.metadata["namespace"]) == "board"
                and e.key == "uuid" and e.metadata["type"] == "string"
                and e.metadata["crc"]["data_original"] == e.metadata["crc"]["data_computed"]):
            value = b"".join(bytes(c.raw) for c in e.children)[: e.data["size"]].rstrip(b"\0")
            if value:
                found.add(hashlib.sha256(value).hexdigest())
    for h in sorted(found):
        print(h)
    return 0 if found else 2


if __name__ == "__main__":
    sys.exit(main())
