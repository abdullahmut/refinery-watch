from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from refinery_watch.flare.firms import (
    BACKFILL_RANGE_DAYS,
    EXPECTED_COLUMNS,
    LIVE_RANGE_DAYS,
    Detection,
    FirmsResponseError,
    area_path,
    availability_path,
    parse_area_csv,
    parse_availability,
    redacted,
    safe_note,
    source_name,
)
from refinery_watch.flare.geometry import BBox
from refinery_watch.sources.http import FetchError, HostBlocked

HANDMADE = Path(__file__).resolve().parents[1] / "fixtures" / "sources" / "handmade"
KEY = "k" * 32
BOX = BBox(-95.3, 29.5, -94.8, 29.9)
HEADER = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,"
    "confidence,version,bright_ti5,frp,daynight\n"
)
Pick = Callable[[str], Path]


def row(**over: str) -> str:
    fields = {
        "latitude": "29.746",
        "longitude": "-95.011",
        "bright_ti4": "330.5",
        "scan": "0.39",
        "track": "0.36",
        "acq_date": "2026-11-11",
        "acq_time": "0742",
        "satellite": "N20",
        "instrument": "VIIRS",
        "confidence": "n",
        "version": "2.0NRT",
        "bright_ti5": "295.1",
        "frp": "10.5",
        "daynight": "N",
    }
    fields.update(over)
    return ",".join(fields.values()) + "\n"


def parse(text: str) -> list[Detection]:
    return list(parse_area_csv(text.encode()))


def test_source_names() -> None:
    assert source_name("NOAA20", "NRT") == "VIIRS_NOAA20_NRT"
    assert source_name("SNPP", "SP") == "VIIRS_SNPP_SP"


def test_area_path_is_exact() -> None:
    path = area_path(KEY, "NOAA20", "NRT", BOX, 2, date(2026, 11, 11))
    assert (
        path
        == f"/api/area/csv/{KEY}/VIIRS_NOAA20_NRT/-95.3000,29.5000,-94.8000,29.9000/2/2026-11-11"
    )


def test_area_path_rounds_to_four_decimals() -> None:
    box = BBox(-95.123456, 29.5, -94.8, 29.987654)
    path = area_path(KEY, "SNPP", "SP", box, 5, date(2026, 1, 2))
    assert "/-95.1235,29.5000,-94.8000,29.9877/5/2026-01-02" in path


@pytest.mark.parametrize("days", [0, 6, -1])
def test_area_path_refuses_a_range_outside_one_to_five(days: int) -> None:
    with pytest.raises(ValueError, match="range_days"):
        area_path(KEY, "SNPP", "NRT", BOX, days, date(2026, 1, 2))


def test_range_constants() -> None:
    assert (LIVE_RANGE_DAYS, BACKFILL_RANGE_DAYS) == (2, 5)


def test_availability_path() -> None:
    assert availability_path(KEY) == f"/api/data_availability/csv/{KEY}/all"


def test_redacted_removes_every_copy() -> None:
    text = f"GET /api/area/csv/{KEY}/x and again {KEY}!"
    out = redacted(text, KEY)
    assert KEY not in out
    assert out.count("REDACTED") == 2


def test_redacted_with_an_empty_key_changes_nothing() -> None:
    assert redacted("abc", "") == "abc"


def test_header_only_body_yields_nothing() -> None:
    assert parse(HEADER) == []
    assert list(parse_area_csv((HANDMADE / "firms_area_empty_handmade.csv").read_bytes())) == []


def test_a_text_reply_for_a_bad_key_raises() -> None:
    with pytest.raises(FirmsResponseError, match="missing columns"):
        parse_area_csv(b"Invalid MAP_KEY.")
    with pytest.raises(FirmsResponseError):
        parse_area_csv((HANDMADE / "firms_area_bad_key_handmade.txt").read_bytes())


def test_missing_columns_fail_loudly_and_say_nothing_about_the_body() -> None:
    body = (HANDMADE / "firms_area_missing_columns_handmade.csv").read_bytes()
    with pytest.raises(FirmsResponseError) as info:
        parse_area_csv(body)
    assert "29.746" not in str(info.value)


def test_an_empty_body_raises() -> None:
    with pytest.raises(FirmsResponseError):
        parse_area_csv(b"")


@pytest.mark.parametrize("missing", sorted(EXPECTED_COLUMNS))
def test_each_expected_column_is_required(missing: str) -> None:
    names = [n for n in HEADER.strip().split(",") if n != missing]
    with pytest.raises(FirmsResponseError):
        parse_area_csv((",".join(names) + "\n").encode())


def test_time_is_utc_from_date_and_hhmm() -> None:
    (found,) = parse(HEADER + row())
    assert found.acquired_at == datetime(2026, 11, 11, 7, 42, tzinfo=UTC)
    assert found.acquired_at.isoformat() == "2026-11-11T07:42:00+00:00"


def test_time_without_leading_zeros() -> None:
    (found,) = parse(HEADER + row(acq_time="42"))
    assert found.acquired_at == datetime(2026, 11, 11, 0, 42, tzinfo=UTC)


def test_row_values() -> None:
    (found,) = parse(HEADER + row(confidence="H", daynight="d"))
    assert (found.lat, found.lon) == (29.746, -95.011)
    assert found.frp_mw == Decimal("10.5")
    assert (found.confidence, found.daynight) == ("h", "D")


def test_handmade_area_file_parses() -> None:
    body = (HANDMADE / "firms_area_viirs_noaa20_nrt_handmade.csv").read_bytes()
    found = list(parse_area_csv(body))
    assert len(found) == 4
    assert [d.frp_mw for d in found] == [
        Decimal("10.50"),
        Decimal("3.20"),
        Decimal("1.90"),
        Decimal("8.00"),
    ]


def test_rows_are_read_as_the_caller_iterates() -> None:
    iterator = parse_area_csv((HEADER + row() + row(frp="x")).encode())
    assert next(iterator).frp_mw == Decimal("10.5")
    with pytest.raises(FirmsResponseError, match="bad number"):
        next(iterator)


@pytest.mark.parametrize(
    "bad",
    [
        {"frp": "abc"},
        {"frp": "-1"},
        {"frp": "NaN"},
        {"latitude": "x"},
        {"latitude": "91"},
        {"longitude": "181"},
        {"longitude": "nan"},
        {"acq_date": "2026-13-01"},
        {"acq_date": "yesterday"},
        {"acq_time": "2561"},
        {"acq_time": "07:42"},
        {"acq_time": "12345"},
        {"confidence": "97"},
        {"daynight": "X"},
    ],
)
def test_a_bad_row_raises(bad: dict[str, str]) -> None:
    with pytest.raises(FirmsResponseError):
        parse(HEADER + row(**bad))


def test_a_short_or_long_row_raises() -> None:
    with pytest.raises(FirmsResponseError, match="columns"):
        parse(HEADER + "29.7,-95.0\n")
    with pytest.raises(FirmsResponseError, match="columns"):
        parse(HEADER + row().strip() + ",extra\n")


def test_non_utf8_bytes_raise() -> None:
    with pytest.raises(FirmsResponseError, match="UTF-8"):
        list(parse_area_csv(HEADER.encode() + b"\xff\xfe,1,2\n"))


def test_availability_is_parsed() -> None:
    found = parse_availability((HANDMADE / "firms_data_availability_handmade.csv").read_bytes())
    assert found["VIIRS_NOAA20_SP"] == (date(2018, 1, 1), date(2026, 8, 31))
    assert found["VIIRS_SNPP_NRT"] == (date(2012, 1, 19), date(2026, 11, 12))
    assert len(found) == 4


@pytest.mark.parametrize(
    "body",
    [b"Invalid MAP_KEY.", b"data_id,min_date,max_date\nX,2020-01-01,soon\n", b""],
)
def test_bad_availability_raises(body: bytes) -> None:
    with pytest.raises(FirmsResponseError):
        parse_availability(body)


def test_safe_note_has_reason_host_and_status_but_not_the_path() -> None:
    url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{KEY}/VIIRS_SNPP_NRT/1,2,3,4/2"
    exc = FetchError("http_status", url, 503)
    assert KEY in str(exc)
    note = safe_note(exc, KEY)
    assert KEY not in note
    assert "api/area" not in note
    assert note == "FetchError: http_status host=firms.modaps.eosdis.nasa.gov status=503"


def test_safe_note_for_a_blocked_host_without_status() -> None:
    exc = HostBlocked("host_blocked", f"https://firms.modaps.eosdis.nasa.gov/api/{KEY}")
    assert safe_note(exc, KEY) == "HostBlocked: host_blocked host=firms.modaps.eosdis.nasa.gov"


def test_safe_note_redacts_even_a_key_in_the_host() -> None:
    exc = FetchError("odd", f"https://{KEY}.example.com/x")
    assert KEY not in safe_note(exc, KEY)


# Swap target: the real FIRMS reply. The handmade file stands in until it is fetched.
def test_real_area_reply_parses(real_fixture: Pick) -> None:
    path = real_fixture("firms/firms_area_viirs_noaa20_nrt_*.csv")
    detections = list(parse_area_csv(path.read_bytes()))
    assert all(d.frp_mw >= 0 for d in detections)
