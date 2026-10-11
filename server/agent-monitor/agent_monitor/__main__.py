"""Entry point: `python -m agent_monitor` (uvicorn, one worker)."""
import sys

import uvicorn

from .app import create_app
from .config import ConfigError, load_settings


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="warning",
                access_log=False, timeout_graceful_shutdown=3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
