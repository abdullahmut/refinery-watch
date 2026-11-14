import csv
import json
from pathlib import Path

import pytest

HANDMADE = Path(__file__).resolve().parents[1] / "fixtures" / "sources" / "handmade"
VIIRS_COLUMNS = [
    "latitude",
    "longitude",
    "bright_ti4",
    "scan",
    "track",
    "acq_date",
    "acq_time",
    "satellite",
    "instrument",
    "confidence",
    "version",
    "bright_ti5",
    "frp",
    "daynight",
]


@pytest.mark.parametrize("path", sorted(HANDMADE.glob("overpass_*.json")), ids=lambda p: p.name)
def test_overpass_fixtures_are_json_objects(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data["elements"], list)


@pytest.mark.parametrize("path", sorted(HANDMADE.glob("firms_*.*")), ids=lambda p: p.name)
def test_firms_fixtures_are_labelled_handmade(path: Path) -> None:
    assert path.stem.endswith("_handmade")


@pytest.mark.parametrize(
    "name", ["firms_area_viirs_noaa20_nrt_handmade.csv", "firms_area_viirs_snpp_nrt_handmade.csv"]
)
def test_area_fixtures_use_the_documented_viirs_columns(name: str) -> None:
    with (HANDMADE / name).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0]) == VIIRS_COLUMNS
    assert all(len(row) == len(VIIRS_COLUMNS) for row in rows)


def test_the_synthetic_label_is_in_the_readme() -> None:
    text = (HANDMADE / "README.md").read_text(encoding="utf-8")
    assert "synthetic" in text
    assert "not confirmed" in text
