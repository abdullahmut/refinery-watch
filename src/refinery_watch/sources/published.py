"""Publication times as UTC plus an IANA zone name for raw_document.published_tz."""

import re
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
MIN_YEAR: Final = 1900
_RFC_YEAR = re.compile(r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+(\d{4})\b")
_AST = re.compile(r"\bAST\s*$", re.IGNORECASE)
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


def _from_datetime(dt: datetime) -> Published | None:
    if dt.year < MIN_YEAR:
        return None
    offset = dt.utcoffset()
    if offset is None:
        return Published(dt.replace(tzinfo=UTC), None, False)
    try:
        return Published(dt.astimezone(UTC), tz_name_for_offset(offset), True)
    except OverflowError:
        return None


def parse_rfc822(value: str) -> Published | None:
    """AST is ambiguous (Atlantic UTC-4, Arabia UTC+3), so it is read as no stated zone."""
    text = value.strip()
    year = _RFC_YEAR.search(text)
    if year is not None and int(year.group(1)) < MIN_YEAR:
        return None
    text = _AST.sub("-0000", text)
    try:
        return _from_datetime(parsedate_to_datetime(text))
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def parse_iso8601(value: str) -> Published | None:
    try:
        return _from_datetime(datetime.fromisoformat(value.strip()))
    except (ValueError, OverflowError):
        return None


def plausible(p: Published, fetched_at: datetime) -> bool:
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("fetched_at must be timezone-aware")
    return p.at <= fetched_at + timedelta(days=1)
