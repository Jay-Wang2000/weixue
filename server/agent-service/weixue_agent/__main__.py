"""Entry point: `python -m weixue_agent`."""
import asyncio
import contextlib
import os
import sys
import warnings

import uvicorn
from starlette.applications import Starlette

from . import jsonlog
from .api import Api
from .config import ConfigError, load_settings, scrub_environment
from .service import Service, boot_tunnel_iface, install_sdk_logging


def build_app(service: Service) -> Starlette:
    @contextlib.asynccontextmanager
    async def lifespan(app):
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    return Starlette(routes=Api(service).routes(), lifespan=lifespan)


def main() -> int:
    jsonlog.set_level(os.environ.get("AGENT_LOG_LEVEL", "info"))
    try:
        settings = load_settings(os.environ)
    except ConfigError as e:
        jsonlog.log("configuration error", level="error", detail=str(e))
        return 2
    jsonlog.register_secrets([settings.token, settings.monitor_token, settings.ha_mcp_url,
                             os.environ.get("ANTHROPIC_API_KEY")])
    if settings.ha_mcp_url:
        jsonlog.register_secret(settings.ha_mcp_url.rsplit("/", 1)[-1])
    dropped = scrub_environment()
    jsonlog.log("environment scrubbed", detail=f"{len(dropped)} variables dropped")
    install_sdk_logging()
    warnings.showwarning = lambda message, category, *args, **kwargs: jsonlog.log(
        "sdk warning", level="info", code=category.__name__, detail=jsonlog.truncate(str(message), 200))
    service = Service(settings, require_iface=boot_tunnel_iface())
    config = uvicorn.Config(build_app(service), host=settings.host, port=settings.port, log_level="warning",
                            access_log=False, timeout_graceful_shutdown=5)
    asyncio.run(uvicorn.Server(config).serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
