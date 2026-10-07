"""OTA response clock: server_time gains `tz`, the POSIX TZ rule of `TZ` in server/.env
(WEIXUE_TZ, empty when unset), so the device keeps a UTC clock and follows daylight saving time."""
import json
import os
import re

ZONEINFO = "/usr/share/zoneinfo"
_POSIX_TZ = re.compile(r"^[A-Za-z<][^,]*\d")


def posix_tz(name=None, zoneinfo=ZONEINFO):
    """POSIX TZ string for the IANA zone name (default $WEIXUE_TZ): the footer line of its TZif v2+
    file. A name that is already a POSIX rule is returned as is; None when neither applies."""
    name = (os.environ.get("WEIXUE_TZ", "") if name is None else name).lstrip(":")
    if not name:
        return None
    root = os.path.realpath(zoneinfo)
    path = os.path.realpath(os.path.join(root, name))
    if path.startswith(root + os.sep) and os.path.isfile(path):
        with open(path, "rb") as f:
            data = f.read()
        if data[:4] == b"TZif" and data[4:5] >= b"2" and data.endswith(b"\n"):
            footer = data[:-1].rsplit(b"\n", 1)[-1]
            try:
                text = footer.decode("ascii")
            except UnicodeDecodeError:
                return None
            return text or None
        return None
    return name if _POSIX_TZ.match(name) else None


def add_tz(handle_post, tz):
    """Wraps OTAHandler.handle_post: a 200 JSON response with a server_time object gets
    server_time.tz. The wrapper keeps the wrapped function's markers and adds `weixue_tz`."""

    async def wrapper(self, request):
        response = await handle_post(self, request)
        if not tz or response.status != 200 or response.content_type != "application/json":
            return response
        try:
            body = json.loads(response.text)
        except (TypeError, ValueError):
            return response
        server_time = body.get("server_time") if isinstance(body, dict) else None
        if isinstance(server_time, dict):
            server_time["tz"] = tz
            response.text = json.dumps(body, separators=(",", ":"))
        return response

    wrapper.__dict__.update(handle_post.__dict__)
    wrapper.weixue_tz = handle_post
    return wrapper


def apply():
    from config.logger import setup_logging
    from core.api.ota_handler import OTAHandler

    if getattr(OTAHandler.handle_post, "weixue_tz", None) is not None:
        return
    tz = posix_tz()
    setup_logging().bind(tag=__name__).info(f"OTA server_time.tz: {tz}")
    OTAHandler.handle_post = add_tz(OTAHandler.handle_post, tz)
