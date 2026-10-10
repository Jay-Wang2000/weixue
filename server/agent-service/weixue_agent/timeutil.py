"""RFC 3339 UTC timestamps with millisecond precision."""
from datetime import datetime, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    dt = dt or now()
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def from_epoch(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, timezone.utc)
