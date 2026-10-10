"""Prefixed ULIDs and trace ids."""
import os
import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))


def new_id(prefix: str) -> str:
    return f"{prefix}_{ulid()}"


def trace_id() -> str:
    return secrets.token_hex(16)
