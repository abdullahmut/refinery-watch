"""Publication times as UTC plus an IANA zone name for raw_document.published_tz."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Final

FIXED_ZONES: Final = {
    timedelta(hours=5, minutes=30): "Asia/Kolkata",
    timedelta(hours=5, minutes=45): "Asia/Kathmandu",
    timedelta(hours=6, minutes=30): "Asia/Yangon",
    timedelta(hours=3, minutes=30): "Asia/Tehran",
    timedelta(hours=9, minutes=30): "Australia/Darwin",
}
MIN_WHOLE_HOURS: Final = -12
MAX_WHOLE_HOURS: Final = 14


@dataclass(frozen=True, slots=True)
class Published:
    at: datetime
    tz: str | None
    zone_stated: bool


def tz_name_for_offset(offset: timedelta) -> str | None:
    """IANA name for a fixed offset. Etc/GMT names carry the inverted sign."""
    if offset == timedelta(0):
        return "UTC"
    if offset in FIXED_ZONES:
        return FIXED_ZONES[offset]
    hours, rest = divmod(offset, timedelta(hours=1))
    if rest == timedelta(0) and MIN_WHOLE_HOURS <= hours <= MAX_WHOLE_HOURS:
        return f"Etc/GMT{-hours:+d}"
    return None


def _from_datetime(dt: datetime) -> Published:
    offset = dt.utcoffset()
    if offset is None:
        return Published(dt.replace(tzinfo=UTC), None, False)
    return Published(dt.astimezone(UTC), tz_name_for_offset(offset), True)


def parse_rfc822(value: str) -> Published | None:
    try:
        return _from_datetime(parsedate_to_datetime(value.strip()))
    except (TypeError, ValueError, IndexError):
        return None


def parse_iso8601(value: str) -> Published | None:
    try:
        return _from_datetime(datetime.fromisoformat(value.strip()))
    except ValueError:
        return None


def plausible(p: Published, fetched_at: datetime) -> bool:
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("fetched_at must be timezone-aware")
    return p.at <= fetched_at + timedelta(days=1)
