import os
from datetime import UTC, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_LOCAL_TIMEZONE = "Europe/London"
LOCAL_TIMEZONE_ENV = "TELOS_LOCAL_TIMEZONE"


@lru_cache(maxsize=16)
def local_timezone(name: str | None = None) -> ZoneInfo:
    configured = (name or os.getenv(LOCAL_TIMEZONE_ENV) or DEFAULT_LOCAL_TIMEZONE).strip()
    zone_name = configured or DEFAULT_LOCAL_TIMEZONE
    try:
        return ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_LOCAL_TIMEZONE)


def local_now() -> datetime:
    return datetime.now(local_timezone())


def local_datetime_from_timestamp(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, local_timezone())


def format_local_timestamp(value: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value[:16].replace("T", " ")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(local_timezone()).strftime(fmt)
