"""Weekday resolution against a publication date."""

from datetime import date, timedelta
from typing import Literal

Tense = Literal["past", "future", "last", "next"]


def resolve_weekday(pub: date, weekday: int, tense: Tense) -> date:
    """Resolves a named weekday (0 is Monday) to a date.

    past and future include the publication date itself. last and next never do.
    """
    if not 0 <= weekday <= 6:
        raise ValueError(f"weekday must be 0 to 6, got {weekday}")
    back = (pub.weekday() - weekday) % 7
    forward = (weekday - pub.weekday()) % 7
    if tense == "past":
        return pub - timedelta(days=back)
    if tense == "future":
        return pub + timedelta(days=forward)
    if tense == "last":
        return pub - timedelta(days=back or 7)
    if tense == "next":
        return pub + timedelta(days=forward or 7)
    raise ValueError(f"unknown tense: {tense!r}")
