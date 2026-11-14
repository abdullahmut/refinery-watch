from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tests.db.flare_helpers import no_network as no_network
from tests.db.helpers import add_document, insert_raw_outage
from tests.flare.synth import FIRST_DAY, SynthDay, flagged_count, load, quiet_history

from refinery_watch.db.connect import Conn
from refinery_watch.flare.calibrate import (
    DEFAULT_TARGET,
    GRID,
    MIN_PLANT_DAYS,
    CurvePoint,
    NotEnoughHistory,
    choose,
    curve,
    write_threshold,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_network")]

DAYS = 400
LAST = FIRST_DAY + timedelta(days=DAYS - 1)


def as_of() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


def rate(history: list[SynthDay], z_min: Decimal, min_detections: int = 1) -> Decimal:
    flagged = flagged_count(history, float(z_min), min_detections)
    return (Decimal(flagged) * Decimal("30.4375") / Decimal(len(history))).quantize(
        Decimal("0.0001")
    )


def by_hand(history: list[SynthDay], target: Decimal, min_detections: int) -> Decimal:
    return next(z for z in GRID if rate(history, z, min_detections) <= target)


def test_grid_runs_from_3_to_10_in_quarters() -> None:
    assert len(GRID) == 29
    assert (GRID[0], GRID[1], GRID[-1]) == (Decimal("3.00"), Decimal("3.25"), Decimal("10.00"))
    assert {"signal": Decimal("1.0"), "alert": Decimal("0.2")} == DEFAULT_TARGET
    assert MIN_PLANT_DAYS == 1000


def test_the_curve_has_29_points_and_the_rate_never_rises(db: Conn) -> None:
    history = quiet_history(DAYS)
    load(db, history)
    points = curve(db, "signal", FIRST_DAY, LAST, as_of())
    assert [p.z_min for p in points] == list(GRID)
    rates = [p.false_alarms_per_plant_month for p in points]
    assert rates == sorted(rates, reverse=True)
    assert all(p.quiet_plant_days == len(history) == 1200 for p in points)
    for p in points:
        assert p.flagged_quiet_days == flagged_count(history, float(p.z_min))
        assert p.false_alarms_per_plant_month == rate(history, p.z_min)
    assert points[0].flagged_quiet_days == 68
    assert points[-1].flagged_quiet_days == 0


def test_choose_returns_the_hand_computed_grid_value(db: Conn) -> None:
    history = quiet_history(DAYS)
    load(db, history)
    signal = curve(db, "signal", FIRST_DAY, LAST, as_of())
    chosen = choose(signal, DEFAULT_TARGET["signal"])
    assert chosen is not None
    assert chosen.z_min == by_hand(history, Decimal("1.0"), 1) == Decimal("3.25")
    alert = curve(db, "alert", FIRST_DAY, LAST, as_of())
    pick = choose(alert, Decimal("0.2"))
    assert pick is not None
    assert pick.z_min == by_hand(history, Decimal("0.2"), 2) == Decimal("5.25")


def test_choose_takes_the_lowest_fitting_value_whatever_the_order() -> None:
    def point(z: str, rate_text: str) -> CurvePoint:
        return CurvePoint(Decimal(z), 1, 1200, Decimal(rate_text))

    points = [point("6.00", "0.1"), point("4.00", "0.3"), point("5.00", "0.2"), point("7.00", "0")]
    best = choose(points, Decimal("0.2"))
    assert best is not None
    assert best.z_min == Decimal("5.00")
    assert choose(points, Decimal("0")) == points[3]
    assert choose(points, Decimal("-1")) is None
    assert choose([], Decimal("1")) is None


def test_under_a_thousand_quiet_plant_days_refuses(db: Conn) -> None:
    load(db, quiet_history(100, ("R9001", "R9002")))
    with pytest.raises(NotEnoughHistory, match="200 quiet"):
        curve(db, "signal", FIRST_DAY, LAST, as_of())
    before = db.execute("SELECT count(*) AS n FROM flare_threshold").fetchone()
    assert before == {"n": 2}


def test_exactly_a_thousand_is_enough(db: Conn) -> None:
    load(db, quiet_history(500, ("R9001", "R9002")))
    points = curve(db, "signal", FIRST_DAY, FIRST_DAY + timedelta(days=499), as_of())
    assert points[0].quiet_plant_days == 1000


def test_days_near_an_outage_or_a_regulator_item_are_not_quiet(db: Conn) -> None:
    history = quiet_history(DAYS)
    load(db, history)
    planted = [h for h in history if h.z == 7.3]
    target = planted[0]
    event = insert_raw_outage(
        db,
        refinery_id=target.refinery_id,
        refinery_unit_id=None,
        whole_plant=True,
        start_date=target.day,
        known_at=datetime.now(UTC) - timedelta(days=1),
    )
    db.execute(
        "INSERT INTO event_source (outage_event_id, raw_document_id, role, source_kind, evidence)"
        " VALUES (%s, %s, 'primary', 'other_media', 'text')",
        (event, add_document(db, 9101)),
    )
    near = [
        h
        for h in history
        if h.refinery_id == target.refinery_id and abs((h.day - target.day).days) <= 7
    ]
    points = curve(db, "signal", FIRST_DAY, LAST, as_of())
    assert points[0].quiet_plant_days == len(history) - len(near)
    assert points[0].flagged_quiet_days == flagged_count(history, 3.0) - sum(
        1 for h in near if h.z >= 3.0
    )
    db.execute(
        "INSERT INTO tceq_event (incident_id, report_stage, raw_document_id, refinery_id,"
        " regulated_entity, event_kind, started_at, report_url)"
        " VALUES ('T1', 'initial', %s, %s, 'Plant', 'Emissions', %s, 'https://x.test')",
        (
            add_document(db, 9102),
            "R9002" if target.refinery_id != "R9002" else "R9003",
            datetime.combine(FIRST_DAY + timedelta(days=200), datetime.min.time(), tzinfo=UTC),
        ),
    )
    more = curve(db, "signal", FIRST_DAY, LAST, as_of())
    assert more[0].quiet_plant_days == points[0].quiet_plant_days - 15


def test_the_period_limits_the_days(db: Conn) -> None:
    history = quiet_history(DAYS)
    load(db, history)
    half = FIRST_DAY + timedelta(days=199)
    with pytest.raises(NotEnoughHistory, match="600 quiet"):
        curve(db, "signal", FIRST_DAY, half, as_of())
    points = curve(db, "signal", FIRST_DAY, FIRST_DAY + timedelta(days=DAYS - 1), as_of())
    assert points[0].quiet_plant_days == 1200


def test_rows_known_after_the_time_are_ignored(db: Conn) -> None:
    load(db, quiet_history(DAYS))
    with pytest.raises(NotEnoughHistory):
        curve(db, "signal", FIRST_DAY, LAST, datetime.now(UTC) - timedelta(hours=1))


def test_the_alert_curve_uses_the_alert_minimum_of_detections(db: Conn) -> None:
    history = quiet_history(DAYS)
    load(db, history)
    db.execute(
        "INSERT INTO flare_threshold (kind, z_min, min_detections, method)"
        " VALUES ('alert', 6.0, 4, 'calibrated')"
    )
    points = curve(db, "alert", FIRST_DAY, LAST, as_of())
    assert all(p.flagged_quiet_days == 0 for p in points)


def test_a_naive_time_is_refused(db: Conn) -> None:
    with pytest.raises(ValueError, match="aware"):
        curve(db, "signal", FIRST_DAY, LAST, datetime(2026, 11, 12))  # noqa: DTZ001


def test_write_threshold_adds_one_calibrated_row_and_the_default_stays_as_of(db: Conn) -> None:
    point = CurvePoint(Decimal("5.25"), 2, 1200, Decimal("0.0507"))
    new_id = write_threshold(db, "alert", point, 2, {"target": "0.2", "history_days": "400"})
    rows = db.execute("SELECT * FROM flare_threshold ORDER BY id").fetchall()
    assert len(rows) == 3
    row = next(r for r in rows if r["id"] == new_id)
    assert (row["kind"], row["z_min"], row["min_detections"], row["method"]) == (
        "alert",
        Decimal("5.25"),
        2,
        "calibrated",
    )
    assert row["params"] == {"target": "0.2", "history_days": "400"}
    before = db.execute(
        "SELECT z_min, method FROM flare_threshold_as_of('alert', %s)",
        (row["known_at"] - timedelta(microseconds=1),),
    ).fetchone()
    assert before == {"z_min": Decimal("6.00"), "method": "default"}
    now = db.execute("SELECT z_min, method FROM flare_threshold_as_of('alert', %s)", (as_of(),))
    assert now.fetchone() == {"z_min": Decimal("5.25"), "method": "calibrated"}
    signal = db.execute("SELECT z_min FROM flare_threshold_as_of('signal', %s)", (as_of(),))
    assert signal.fetchone() == {"z_min": Decimal("3.50")}
