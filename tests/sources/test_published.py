from datetime import UTC, datetime, timedelta

import pytest

from refinery_watch.sources.published import (
    Published,
    parse_iso8601,
    parse_rfc822,
    plausible,
    tz_name_for_offset,
)


def utc(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def test_rfc822_gmt() -> None:
    assert parse_rfc822("Tue, 10 Nov 2026 14:00:00 GMT") == Published(
        utc(2026, 11, 10, 14), "UTC", True
    )


def test_rfc822_positive_offset_uses_inverted_etc_name() -> None:
    p = parse_rfc822("Tue, 10 Nov 2026 14:00:00 +0800")
    assert p == Published(utc(2026, 11, 10, 6), "Etc/GMT-8", True)


def test_rfc822_negative_offset() -> None:
    p = parse_rfc822("Tue, 10 Nov 2026 14:00:00 -0500")
    assert p == Published(utc(2026, 11, 10, 19), "Etc/GMT+5", True)


def test_rfc822_named_zone() -> None:
    p = parse_rfc822("Tue, 10 Nov 2026 14:00:00 EST")
    assert p is not None
    assert p.tz == "Etc/GMT+5"
    assert p.at == utc(2026, 11, 10, 19)


def test_rfc822_half_hour_zones() -> None:
    p = parse_rfc822("Tue, 10 Nov 2026 14:00:00 +0530")
    assert p is not None
    assert p.tz == "Asia/Kolkata"
    assert p.at == utc(2026, 11, 10, 8, 30)


def test_rfc822_odd_offset_keeps_exact_instant() -> None:
    p = parse_rfc822("Tue, 10 Nov 2026 14:00:00 -0330")
    assert p == Published(utc(2026, 11, 10, 17, 30), None, True)


def test_rfc822_garbage() -> None:
    assert parse_rfc822("yesterday") is None
    assert parse_rfc822("") is None


def test_iso_without_zone() -> None:
    assert parse_iso8601("2026-11-10T23:30:00") == Published(utc(2026, 11, 10, 23, 30), None, False)


def test_iso_date_only() -> None:
    assert parse_iso8601("2026-11-10") == Published(utc(2026, 11, 10), None, False)


def test_iso_with_offset_and_z() -> None:
    assert parse_iso8601("2026-11-10T20:00:00+08:00") == Published(
        utc(2026, 11, 10, 12), "Etc/GMT-8", True
    )
    assert parse_iso8601("2026-11-10T20:00:00Z") == Published(utc(2026, 11, 10, 20), "UTC", True)


def test_iso_garbage() -> None:
    assert parse_iso8601("yesterday") is None
    assert parse_iso8601("") is None


@pytest.mark.parametrize(
    ("hours", "name"),
    [(0, "UTC"), (14, "Etc/GMT-14"), (-12, "Etc/GMT+12"), (1, "Etc/GMT-1"), (-1, "Etc/GMT+1")],
)
def test_whole_hour_names(hours: int, name: str) -> None:
    assert tz_name_for_offset(timedelta(hours=hours)) == name


def test_fixed_zones_and_unmapped() -> None:
    assert tz_name_for_offset(timedelta(hours=5, minutes=45)) == "Asia/Kathmandu"
    assert tz_name_for_offset(timedelta(hours=6, minutes=30)) == "Asia/Yangon"
    assert tz_name_for_offset(timedelta(hours=3, minutes=30)) == "Asia/Tehran"
    assert tz_name_for_offset(timedelta(hours=9, minutes=30)) == "Australia/Darwin"
    assert tz_name_for_offset(timedelta(hours=-3, minutes=-30)) is None
    assert tz_name_for_offset(timedelta(hours=13)) == "Etc/GMT-13"
    assert tz_name_for_offset(timedelta(hours=-13)) is None
    assert tz_name_for_offset(timedelta(hours=15)) is None


def test_plausible() -> None:
    fetched = utc(2026, 11, 10, 12)
    assert plausible(Published(fetched + timedelta(hours=1), None, True), fetched)
    assert not plausible(Published(fetched + timedelta(days=2), None, True), fetched)


def test_plausible_needs_aware_fetch_time() -> None:
    with pytest.raises(ValueError, match="aware"):
        plausible(Published(utc(2026, 1, 1), None, True), datetime(2026, 1, 1))  # noqa: DTZ001
