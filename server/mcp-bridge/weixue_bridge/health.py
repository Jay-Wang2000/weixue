"""Container healthcheck: exit 0 while the heartbeat file is fresh."""
import os
import sys
import time

from .config import DEFAULT_HEALTH_FILE, HEALTH_MAX_AGE_S


def healthy(path: str, now: float) -> bool:
    try:
        return now - os.stat(path).st_mtime < HEALTH_MAX_AGE_S
    except FileNotFoundError:
        return False


if __name__ == "__main__":
    sys.exit(0 if healthy(os.environ.get("BRIDGE_HEALTH_FILE", DEFAULT_HEALTH_FILE), time.time()) else 1)
