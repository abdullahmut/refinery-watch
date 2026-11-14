from pathlib import Path

import pytest

from refinery_watch.flare.footprints import LocationsError, read_locations

HEADER = "refinery_id,lat,lon,radius_m,osm_ref,source_url,verified\n"


def write(tmp_path: Path, *lines: str, header: str = HEADER) -> Path:
    path = tmp_path / "locations.csv"
    path.write_text(header + "".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def test_valid_rows_are_read(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "R9001,29.745,-95.012,3000,way/12,https://example.test/a,yes",
        "R9002,51.9,4.3,,,,no",
    )
    first, second = read_locations(path)
    assert (first.refinery_id, first.lat, first.lon, first.radius_m) == (
        "R9001",
        29.745,
        -95.012,
        3000,
    )
    assert (first.osm_ref, first.source_url, first.verified) == (
        "way/12",
        "https://example.test/a",
        True,
    )
    assert (second.radius_m, second.osm_ref, second.source_url, second.verified) == (
        2500,
        None,
        None,
        False,
    )


@pytest.mark.parametrize(
    ("row", "line", "text"),
    [
        ("R9001,91,0,,,,no", 2, "lat"),
        ("R9001,-91,0,,,,no", 2, "lat"),
        ("R9001,0,181,,,,no", 2, "lon"),
        ("R9001,x,0,,,,no", 2, "lat is not a number"),
        ("R9001,nan,0,,,,no", 2, "lat"),
        ("R9001,0,0,499,,,no", 2, "radius_m"),
        ("R9001,0,0,10001,,,no", 2, "radius_m"),
        ("R9001,0,0,2.5,,,no", 2, "whole number"),
        ("R9001,0,0,,node/5,,no", 2, "osm_ref"),
        ("R9001,0,0,,way/5x,,no", 2, "osm_ref"),
        ("R9001,0,0,,,,maybe", 2, "verified"),
        ("R9001,0,0,,,ftp://x,no", 2, "source_url"),
        ("X1,0,0,,,,no", 2, "refinery_id"),
        ("R9001,0,0,,,no", 2, "columns"),
        ("R9001,0,0,,,,no,extra", 2, "columns"),
    ],
)
def test_bad_rows_are_refused_with_the_line(tmp_path: Path, row: str, line: int, text: str) -> None:
    with pytest.raises(LocationsError, match=f"line {line}: .*{text}"):
        read_locations(write(tmp_path, row))


def test_the_line_number_is_the_file_line(tmp_path: Path) -> None:
    path = write(tmp_path, "R9001,1,1,,,,no", "R9002,2,2,,,,no", "R9003,95,2,,,,no")
    with pytest.raises(LocationsError, match="line 4"):
        read_locations(path)


def test_a_duplicate_id_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path, "R9001,1,1,,,,no", "R9001,2,2,,,,no")
    with pytest.raises(LocationsError, match="line 3: R9001 appears twice"):
        read_locations(path)


def test_a_missing_column_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path, "R9001,1,1,,no", header="refinery_id,lat,lon,radius_m,verified\n")
    with pytest.raises(LocationsError, match="line 1: header"):
        read_locations(path)


def test_an_unreadable_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(LocationsError, match="UTF-8"):
        read_locations(path)
    with pytest.raises(LocationsError, match="cannot be read"):
        read_locations(tmp_path / "missing.csv")


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(LocationsError, match="header"):
        read_locations(path)


def test_a_file_over_one_megabyte_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path, "R9001,1,1,,,,no")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("#" * 1_000_001)
    with pytest.raises(LocationsError, match="over 1000000 bytes"):
        read_locations(path)


def test_a_file_just_under_the_limit_is_read(tmp_path: Path) -> None:
    path = write(tmp_path, "R9001,1,1,,,,no")
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write("\n" * (1_000_000 - path.stat().st_size))
    assert path.stat().st_size == 1_000_000
    assert len(read_locations(path)) == 1
