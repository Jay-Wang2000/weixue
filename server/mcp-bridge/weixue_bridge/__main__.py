import asyncio
import logging
import sys

from .bridge import run
from .config import ConfigError, load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

try:
    config = load_config()
except ConfigError as e:
    logging.getLogger("weixue_bridge").error("config: %s", e)
    sys.exit(2)
asyncio.run(run(config))
