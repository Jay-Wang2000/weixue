import asyncio
import logging
import sys

from .bridge import BridgeExit, run
from .config import ConfigError, load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("weixue_bridge")

try:
    config = load_config()
except ConfigError as e:
    log.error("config: %s", e)
    sys.exit(2)
try:
    asyncio.run(run(config))
except BridgeExit as e:
    log.error("exiting: %s", e)
    sys.exit(1)
