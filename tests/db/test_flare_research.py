from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest
from tests.db.flare_helpers import add_day, add_footprint
from tests.db.flare_helpers import no_network as no_network
from tests.db.helpers import insert_raw_outage

from refinery_watch.db.connect import Conn
from refinery_watch.flare.research import (
    NO_HISTORY,
    RESULTS_NOTE,
    ResearchFigures,
    figures,
    render_results,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_network")]

FIRST = date(2025, 1, 1)
DAYS = 400
OUTAGE_KNOWN = datetime.now(UTC) - timedelta(days=2)
STARTS = [FIRST + timedelta(days=20 + 35 * i) for i in range(10)]
OFFSETS = [-3, -2, 0, 1, 2, 3, -1]
LEADS = [10, 6, 30, -4, 12, 8, 20]
FALSE_DAYS = [FIRST + timedelta(days=d) for d in (5, 375, 395)]
_docs = iter(range(10_000, 20_000))


def report_time(start: date) -> datetime:
    return datetime(start.year, start.month, start.day, 12, tzinfo=UTC)


def document(conn: Conn, published: datetime) -> int:
    n = next(_docs)
    row = conn.execute(
        "INSERT INTO raw_document (source_id, url, content_sha256, object_key, body_chars,"
        " prefilter_passed, prefilter_terms, published_at)"
        " VALUES ('fixture_src', %s, %s, %s, 10, true, '{refinery}', %s) RETURNING id",
        (f"https://example.test/r{n}", f"{n:064x}", f"raw/fixture_src/r{n}.html", published),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def event(conn: Conn, start: date, event_type: str = "unplanned", **over: object) -> int:
    fields: dict[str, object] = {
        "event_type": event_type,
        "refinery_unit_id": None,
        "whole_plant": True,
        "start_date": start,
        "known_at": OUTAGE_KNOWN,
    }
    fields.update(over)
    found = insert_raw_outage(conn, **fields)
    conn.execute(
        "INSERT INTO event_source (outage_event_id, raw_document_id, role, source_kind, evidence,"
        " known_at) VALUES (%s, %s, 'primary', 'other_media', 'text', %s)",
        (found, document(conn, report_time(start)), fields["known_at"]),
    )
    return found


def build(conn: Conn) -> int:
    """Ten unplanned events, seven with flaring near them, three false alarms. Returns the plant."""
    fp = add_footprint(conn)
    flagged: dict[date, datetime] = {}
    for i, offset in enumerate(OFFSETS):
        flagged[STARTS[i] + timedelta(days=offset)] = report_time(STARTS[i]) - timedelta(
            hours=LEADS[i]
        )
    late = STARTS[0] + timedelta(days=-2)
    for day in FALSE_DAYS:
        flagged[day] = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=27)
    for i in range(DAYS):
        day = FIRST + timedelta(days=i)
        if day == late:
            add_day(
                conn,
                fp,
                day,
                z=5.0,
                n_detections=2,
                backfilled=True,
                available_at=report_time(STARTS[0]) + timedelta(hours=5),
            )
        elif day in flagged:
            hit = day not in FALSE_DAYS
            add_day(
                conn,
                fp,
                day,
                z=5.0 if hit else 4.0,
                n_detections=2 if hit else 1,
                backfilled=True,
                available_at=flagged[day],
            )
        else:
            add_day(conn, fp, day, z=0.5, n_detections=0)
    for start in STARTS:
        event(conn, start)
    event(conn, STARTS[7], "planned")
    event(conn, STARTS[8], "planned")
    event(conn, FIRST - timedelta(days=100))
    return fp


def clock(conn: Conn) -> datetime:
    row = conn.execute("SELECT clock_timestamp() AS t").fetchone()
    assert row is not None
    return row["t"]  # type: ignore[no-any-return]


def test_an_empty_database_renders_the_fixed_text(db: Conn) -> None:
    now = datetime.now(UTC)
    signal = figures(db, now, "signal")
    alert = figures(db, now, "alert")
    assert signal.scored_plant_days == 0
    assert (signal.hit_rate, signal.hit_rate_low, signal.median_lead_hours) == (None, None, None)
    assert signal.false_alarms_per_plant_month is None
    assert signal.backfilled_share is None
    assert (signal.first_day, signal.last_day) == (None, None)
    assert signal.by_region == ()
    assert render_results(signal, alert) == NO_HISTORY + "\n\n" + RESULTS_NOTE
    assert NO_HISTORY == (
        "No FIRMS history is loaded. No hit rate, false alarm rate or lead time is reported."
    )


def test_ten_events_seven_with_flaring(db: Conn) -> None:
    build(db)
    f = figures(db, clock(db) + timedelta(seconds=1), "signal")
    assert (f.kind, f.z_min, f.threshold_method) == ("signal", Decimal("3.50"), "default")
    assert (f.scored_plant_days, f.plants) == (400, 1)
    assert (f.first_day, f.last_day) == (FIRST, FIRST + timedelta(days=399))
    assert (f.unplanned_events, f.planned_events) == (10, 2)
    assert f.hits == 7
    assert f.hit_rate == Decimal("0.7000")
    assert (f.hit_rate_low, f.hit_rate_high) == (Decimal("0.3968"), Decimal("0.8922"))
    assert f.false_alarm_days == 3
    assert f.quiet_plant_months == Decimal("8.21")
    expected = (Decimal(3) * Decimal("30.4375") / Decimal(250)).quantize(
        Decimal("0.0001"), ROUND_HALF_UP
    )
    assert f.false_alarms_per_plant_month == expected == Decimal("0.3653")
    assert f.median_lead_hours == Decimal("10.0")
    assert (f.lead_n, f.flare_first_n) == (7, 6)
    assert f.backfilled_share == Decimal("0.0275")


def test_the_earliest_flagged_day_sets_the_lead(db: Conn) -> None:
    build(db)
    f = figures(db, clock(db) + timedelta(seconds=1), "signal")
    # Event 0 has two flagged days: 10 hours before the news, and 5 hours after it.
    assert f.lead_n == 7
    assert f.median_lead_hours == Decimal("10.0")


def test_group_tables(db: Conn) -> None:
    build(db)
    f = figures(db, clock(db) + timedelta(seconds=1), "signal")
    (region,) = f.by_region
    assert region.key == "US Gulf"
    assert (region.scored_plant_days, region.unplanned_events, region.hits) == (400, 10, 7)
    assert region.hit_rate == Decimal("0.7000")
    assert (region.false_alarm_days, region.quiet_plant_days) == (3, 250)
    assert region.false_alarms_per_plant_month == Decimal("0.3653")
    (kind,) = f.by_footprint_kind
    assert kind.key == "point_radius"
    assert kind.hits == 7


def test_the_alert_threshold_flags_fewer_days(db: Conn) -> None:
    build(db)
    f = figures(db, clock(db) + timedelta(seconds=1), "alert")
    assert (f.z_min, f.threshold_method) == (Decimal("6.00"), "default")
    assert f.hits == 0
    assert f.false_alarm_days == 0
    assert f.hit_rate == Decimal("0.0000")
    assert f.median_lead_hours is None
    assert f.lead_n == 0


def test_a_calibrated_threshold_is_used_from_when_it_was_known(db: Conn) -> None:
    build(db)
    before = clock(db)
    db.execute(
        "INSERT INTO flare_threshold (kind, z_min, min_detections, method)"
        " VALUES ('signal', 4.5, 2, 'calibrated')"
    )
    after = clock(db) + timedelta(seconds=1)
    assert figures(db, before, "signal").hits == 7
    f = figures(db, after, "signal")
    assert (f.z_min, f.threshold_method) == (Decimal("4.50"), "calibrated")
    assert f.hits == 7
    assert f.false_alarm_days == 0


def test_rows_known_after_the_time_change_nothing(db: Conn) -> None:
    fp = build(db)
    mark = clock(db)
    first = figures(db, mark, "signal")
    event(db, FIRST + timedelta(days=200), known_at=clock(db))
    add_day(db, fp, FIRST + timedelta(days=400), z=9.0, n_detections=5)
    db.execute(
        "INSERT INTO flare_threshold (kind, z_min, min_detections, method)"
        " VALUES ('signal', 9.0, 9, 'calibrated')"
    )
    assert figures(db, mark, "signal") == first
    later = figures(db, clock(db) + timedelta(seconds=1), "signal")
    assert later != first
    assert later.unplanned_events == 11
    assert later.scored_plant_days == 401


def test_an_event_without_a_footprint_or_a_scored_start_day_is_not_counted(db: Conn) -> None:
    build(db)
    event(db, STARTS[0], refinery_id="R9002", refinery_unit_id=None, whole_plant=True)
    f = figures(db, clock(db) + timedelta(seconds=1), "signal")
    assert f.unplanned_events == 10


def test_a_regulator_item_makes_nearby_days_not_quiet(db: Conn) -> None:
    build(db)
    when = datetime.combine(FALSE_DAYS[0], datetime.min.time(), tzinfo=UTC)
    db.execute(
        "INSERT INTO tceq_event (incident_id, report_stage, raw_document_id, refinery_id,"
        " regulated_entity, event_kind, started_at, report_url)"
        " VALUES ('T1', 'initial', %s, 'R9001', 'Plant', 'Emissions', %s, 'https://x.test')",
        (document(db, when), when),
    )
    f = figures(db, clock(db) + timedelta(seconds=1), "signal")
    assert f.false_alarm_days == 2
    assert f.quiet_plant_months == (Decimal(250 - 13) / Decimal("30.4375")).quantize(
        Decimal("0.01"), ROUND_HALF_UP
    )


def test_a_naive_time_is_refused(db: Conn) -> None:
    with pytest.raises(ValueError, match="aware"):
        figures(db, datetime(2026, 11, 12), "signal")  # noqa: DTZ001


def test_the_rendered_results_carry_every_figure(db: Conn) -> None:
    build(db)
    now = clock(db) + timedelta(seconds=1)
    text = render_results(figures(db, now, "signal"), figures(db, now, "alert"))
    for needle in (
        "# Flare signal results",
        "## Signal threshold",
        "## Alert threshold",
        "Score at or over 3.50 (default).",
        "Scored plant-days: 400 at 1 plants",
        "Hits: 7 of 10. Hit rate 0.7000, Wilson 95 percent interval 0.3968 to 0.8922.",
        "median 10.0 hours over 7 events. The flare came first in 6.",
        "A negative lead means the news came first.",
        "3 flagged quiet plant-days over 8.21 quiet plant-months, a rate of 0.3653",
        "| US Gulf | 400 | 10 | 7 | 0.7000 | 3 | 0.3653 |",
        "| point_radius | 400 | 10 | 7 | 0.7000 | 3 | 0.3653 |",
        "upper bound",
        "OpenStreetMap contributors",
        "2025-01-01 to 2026-02-04",
    ):
        assert needle in text, needle


def test_render_shows_n_a_for_missing_figures() -> None:
    now = datetime(2026, 11, 12, tzinfo=UTC)
    blank = ResearchFigures(
        as_of=now,
        kind="signal",
        z_min=Decimal("3.50"),
        threshold_method="default",
        scored_plant_days=10,
        plants=1,
        first_day=date(2026, 1, 1),
        last_day=date(2026, 1, 10),
        unplanned_events=0,
        planned_events=0,
        hits=0,
        hit_rate=None,
        hit_rate_low=None,
        hit_rate_high=None,
        false_alarm_days=0,
        quiet_plant_months=None,
        false_alarms_per_plant_month=None,
        median_lead_hours=None,
        lead_n=0,
        flare_first_n=0,
        by_region=(),
        by_footprint_kind=(),
        backfilled_share=None,
    )
    text = render_results(blank, replace(blank, kind="alert"))
    assert "Hit rate n/a, Wilson 95 percent interval n/a to n/a." in text
    assert "median n/a hours over 0 events" in text
    assert "a rate of n/a per plant-month" in text


def _flags(conn: Conn, flagged: dict[date, datetime]) -> None:
    fp = add_footprint(conn)
    for i in range(130):
        day = FIRST + timedelta(days=i)
        if day in flagged:
            add_day(
                conn, fp, day, z=5.0, n_detections=2, backfilled=True, available_at=flagged[day]
            )
        else:
            add_day(conn, fp, day, z=0.5, n_detections=0)


def test_a_flag_four_days_after_the_start_is_not_a_hit(db: Conn) -> None:
    start = FIRST + timedelta(days=100)
    late = start + timedelta(days=4)
    _flags(db, {late: datetime.combine(late, datetime.min.time(), tzinfo=UTC)})
    event(db, start)
    f = figures(db, clock(db) + timedelta(seconds=1), "signal")
    assert (f.unplanned_events, f.hits) == (1, 0)


def test_a_source_known_after_the_time_does_not_move_the_first_report(db: Conn) -> None:
    start = FIRST + timedelta(days=100)
    _flags(db, {start: report_time(start) - timedelta(hours=10)})
    found = event(db, start)
    db.execute(
        "INSERT INTO event_source (outage_event_id, raw_document_id, role, source_kind, evidence,"
        " known_at) VALUES (%s, %s, 'corroboration', 'other_media', 'text', %s)",
        (
            found,
            document(db, report_time(start) - timedelta(hours=30)),
            datetime.now(UTC) + timedelta(days=1),
        ),
    )
    f = figures(db, clock(db) + timedelta(seconds=1), "signal")
    assert f.median_lead_hours == Decimal("10.0")


def test_the_results_always_carry_the_fixed_limits_line(db: Conn) -> None:
    now = datetime.now(UTC)
    empty = render_results(figures(db, now, "signal"), figures(db, now, "alert"))
    assert empty == NO_HISTORY + "\n\n" + RESULTS_NOTE
    build(db)
    later = clock(db) + timedelta(seconds=1)
    full = render_results(figures(db, later, "signal"), figures(db, later, "alert"))
    assert full.count(RESULTS_NOTE) == 1
    assert RESULTS_NOTE == (
        "Signal only, not capacity. Lead times on backfilled days assume detections were"
        " available 3 hours after the pass. Limits: docs/research/flare-lead-time.md."
    )
