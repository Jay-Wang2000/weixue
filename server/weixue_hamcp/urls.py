"""ha-mcp URL parsing; error messages never echo the URL (the secret path is the credential)."""
from urllib.parse import urlsplit


class UrlError(ValueError):
    pass


def redact(url: str) -> str:
    """scheme://host:port of a URL, dropping the secret path and query."""
    parts = urlsplit(url)
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


def parse_ha_mcp_url(raw: str) -> str:
    url = raw.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UrlError("HA_MCP_URL must be http(s)://<host>:<port>/<secret path>")
    if parts.path in ("", "/"):
        raise UrlError("HA_MCP_URL has no secret path")
    if parts.query or parts.fragment:
        raise UrlError("HA_MCP_URL must not carry a query or fragment")
    return url
