"""Container healthcheck: `python -m weixue_agent.health` exits 0 while /healthz answers ok."""
import json
import os
import sys
import urllib.request


def main() -> int:
    port = os.environ.get("AGENT_PORT", "8010")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=4) as resp:
            return 0 if json.loads(resp.read()).get("ok") else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
