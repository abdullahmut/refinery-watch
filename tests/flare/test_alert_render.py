import os
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from refinery_watch.flare.alert import AlertCandidate, render
from refinery_watch.flare.support import Evidence

SNAPSHOTS = Path(__file__).resolve().parents[1] / "fixtures" / "flare_alerts"
NOW = datetime(2026, 11, 12, 5, 38, tzinfo=UTC)

BASE = AlertCandidate(
    flare_day_id=41,
    refinery_id="R9001",
    name="Testville Refinery",
    country="United States",
    region="US Gulf",
    flare_date=date(2026, 11, 12),
    n_detections=5,
    frp_sum_mw=Decimal("120.5"),
    median_frp_mw=Decimal("3.2"),
    ratio=Decimal("28.9"),
    z=Decimal("6.72"),
    baseline_days=88,
    z_min=Decimal("6.00"),
    threshold_method="default",
    footprint_kind="osm_polygon",
    radius_m=None,
    evidence=(),
)
NEWS = Evidence("outage_near", 7, date(2026, 11, 11), "unplanned", "CDU 1", "medium")
OPEN = Evidence("outage_open", 8, date(2026, 10, 2), "planned", None, "low")
REGULATOR = Evidence("regulator_near", 9, date(2026, 11, 12), "Emissions event", "flare", None)
FOURTH = Evidence("regulator_near", 10, date(2026, 11, 12), "Upset", None, None)

CASES = {
    "ratio_shown": BASE,
    "usually_dark": replace(
        BASE, median_frp_mw=Decimal("0.0"), ratio=None, z=Decimal("9.60"), baseline_days=90
    ),
    "under_one_megawatt": replace(
        BASE, median_frp_mw=Decimal("0.6"), ratio=None, z=Decimal("9.50")
    ),
    "approximate_circle": replace(
        BASE, footprint_kind="point_radius", radius_m=2500, threshold_method="calibrated"
    ),
    "evidence_two_items": replace(BASE, evidence=(NEWS, REGULATOR)),
    "bounds": replace(BASE, footprint_kind="osm_bounds"),
}


def check(name: str, suffix: str, actual: str) -> None:
    path = SNAPSHOTS / f"{name}.{suffix}"
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        SNAPSHOTS.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8", newline="\n")
    assert actual == path.read_text(encoding="utf-8"), f"{path.name} differs"


# ratio_shown has no evidence, so it is also the snapshot for the evidence-none case.
@pytest.mark.parametrize("name", sorted(CASES))
def test_text_and_html_match_the_snapshot(name: str) -> None:
    subject, text, page = render(CASES[name], NOW)
    check(name, "subject.txt", subject + "\n")
    check(name, "body.txt", text + "\n")
    check(name, "body.html", page + "\n")


def test_the_subject_is_one_line_with_the_short_date() -> None:
    subject, _, _ = render(BASE, NOW)
    assert subject == "Flaring alert: Testville Refinery, United States, 12 Nov"


def test_a_name_with_line_breaks_cannot_split_the_subject() -> None:
    tricky = replace(BASE, name="Test\r\nBcc: x@y.test", country="U\nS")
    subject, text, _ = render(tricky, NOW)
    assert "\r" not in subject
    assert "\n" not in subject
    assert "Bcc: x@y.test, U S" in subject
    assert text.splitlines()[0].startswith("Test Bcc: x@y.test")


def test_no_address_and_no_key_in_any_body() -> None:
    for case in CASES.values():
        subject, text, page = render(case, NOW)
        for part in (subject, text, page):
            assert "@" not in part
            assert "re_" not in part


def test_html_is_escaped_and_uses_inline_styles_only() -> None:
    hostile = replace(BASE, name="<script>alert(1)</script> & Co")
    _, text, page = render(hostile, NOW)
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    assert "&amp; Co" in page
    assert "<script>" in text
    assert "<style" not in page
    assert "class=" not in page
    assert 'style="' in page


def test_at_most_three_items_are_listed() -> None:
    many = replace(BASE, evidence=(NEWS, OPEN, REGULATOR, FOURTH))
    _, text, _ = render(many, NOW)
    assert text.count(", confidence ") == 3
    assert "Upset" not in text


def test_a_regulator_item_has_no_confidence_to_state() -> None:
    _, text, _ = render(replace(BASE, evidence=(REGULATOR,)), NOW)
    assert "Regulator record: Emissions event at flare, start 2026-11-12, confidence n/a." in text


def test_an_outage_flagged_whole_plant_reads_as_whole_plant() -> None:
    whole = replace(OPEN, whole_plant=True)
    _, text, _ = render(replace(BASE, evidence=(whole,)), NOW)
    assert "Open outage: planned at whole plant, start 2026-10-02, confidence low." in text


def test_an_outage_with_no_unit_label_and_no_flag_reads_as_unit_not_stated() -> None:
    _, text, _ = render(replace(BASE, evidence=(OPEN,)), NOW)
    assert "Open outage: planned at unit not stated, start 2026-10-02, confidence low." in text


def test_item_text_is_cleaned_to_one_short_line() -> None:
    hostile = replace(
        NEWS,
        unit_label="CDU\r\n\u202eevil\x00" + "x" * 200,
        event_type="un\u2066planned\n",
    )
    _, text, page = render(replace(BASE, evidence=(hostile,)), NOW)
    line = next(ln for ln in text.splitlines() if ln.startswith("Outage near the date"))
    both = text + page
    assert "\u202e" not in both
    assert "\u2066" not in both
    assert "\x00" not in both
    assert "unplanned at CDU evilxxx" in line
    unit = line.split(" at ", 1)[1].split(", start", 1)[0]
    assert len(unit) == 80


def test_a_median_of_zero_says_no_detections_and_a_small_one_does_not() -> None:
    dark = render(CASES["usually_dark"], NOW)[1]
    small = render(CASES["under_one_megawatt"], NOW)[1]
    assert "It usually shows no detections." in dark
    assert "Its normal is under 1 MW a day." in small
    assert "usually shows no detections" not in small


def test_none_found_names_the_dubai_time() -> None:
    _, text, _ = render(BASE, datetime(2026, 11, 12, 21, 5, tzinfo=UTC))
    assert "none found as of 01:05 Dubai time." in text


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        ({"n_detections": 9}, "detections on 2026-11-12 (UTC day): 9,"),
        ({"frp_sum_mw": Decimal("77.7")}, "power 77.7 MW."),
        ({"median_frp_mw": Decimal("5.5")}, "median is 5.5 MW"),
        ({"baseline_days": 61}, "over 61 scored days"),
        ({"ratio": Decimal("12.3")}, "about 12.3 times normal"),
        ({"z": Decimal("9.99")}, "Score 9.99,"),
        ({"z_min": Decimal("7.25")}, "threshold 7.25 ("),
        ({"threshold_method": "calibrated"}, "(calibrated)"),
        ({"region": "NW Europe"}, ", NW Europe."),
        ({"radius_m": 1800, "footprint_kind": "point_radius"}, "a 1800 m circle"),
    ],
)
def test_every_figure_in_the_text_comes_from_the_candidate(
    change: dict[str, object], needle: str
) -> None:
    _, text, _ = render(replace(BASE, **change), NOW)  # type: ignore[arg-type]
    assert needle in text
    assert needle not in render(BASE, NOW)[1]


def test_render_refuses_a_naive_time() -> None:
    with pytest.raises(ValueError, match="aware"):
        render(BASE, datetime(2026, 11, 12, 5, 38))  # noqa: DTZ001
