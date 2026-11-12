import math
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.db.flare_helpers import (
    add_coverage,
    add_detection,
    add_footprint,
    current_day,
    seed_days,
    write_day,
)
from tests.db.flare_helpers import no_network as no_network
from tests.db.helpers import add_document, open_in_schema

from refinery_watch.db.connect import Conn
from refinery_watch.flare.score import write_days

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_network")]

FIRST = date(2026, 1, 1)
TARGET = FIRST + timedelta(days=90)


def history(conn: Conn, values: Sequence[float | None], footprint: int | None = None) -> int:
    """Seeds and scores the days in order. Returns the footprint id."""
    fp = footprint if footprint is not None else add_footprint(conn)
    for day in seed_days(conn, fp, FIRST, values):
        write_day(conn, day)
    return fp


def test_median_mad_and_z_for_alternating_zero_and_nine(app_db: Conn) -> None:
    fp = history(app_db, [0.0 if i % 2 == 0 else 9.0 for i in range(90)])
    seed_days(app_db, fp, TARGET, [99.0])
    assert write_day(app_db, TARGET) is not None
    row = current_day(app_db, TARGET)
    assert row["baseline_median"] == pytest.approx(math.log(10) / 2, abs=1e-6)
    assert row["baseline_mad"] == pytest.approx(math.log(10) / 2, abs=1e-6)
    assert row["baseline_days"] == 90
    assert row["x"] == pytest.approx(math.log(100), abs=1e-9)
    assert 1.4826 * row["baseline_mad"] == pytest.approx(1.706906, abs=1e-5)
    assert round(row["z"], 3) == 2.023


def test_a_dark_plant_with_twelve_megawatts_scores_5_13(app_db: Conn) -> None:
    fp = history(app_db, [0.0] * 90)
    seed_days(app_db, fp, TARGET, [12.0])
    write_day(app_db, TARGET)
    row = current_day(app_db, TARGET)
    assert (row["baseline_median"], row["baseline_mad"]) == (0.0, 0.0)
    assert round(row["z"], 3) == 5.130
    assert row["z"] == pytest.approx(math.log(13) / 0.5)


@pytest.mark.parametrize(("scored", "has_z"), [(59, False), (60, True)])
def test_z_needs_sixty_scored_days(app_db: Conn, scored: int, has_z: bool) -> None:
    fp = history(app_db, [1.0] * scored)
    target = FIRST + timedelta(days=scored)
    seed_days(app_db, fp, target, [5.0])
    write_day(app_db, target)
    row = current_day(app_db, target)
    assert row["baseline_days"] == scored
    assert (row["z"] is not None) is has_z


def test_days_missing_from_the_window_are_not_zero(app_db: Conn) -> None:
    fp = add_footprint(app_db)
    values = [9.0] * 70 + [None] * 20 + [9.0]
    for day in seed_days(app_db, fp, FIRST, values):
        write_day(app_db, day)
    row = current_day(app_db, FIRST + timedelta(days=90))
    assert row["baseline_days"] == 70
    assert row["baseline_median"] == pytest.approx(math.log(10))
    assert row["z"] == pytest.approx(0.0)


def test_the_window_is_ninety_days_before_the_day(app_db: Conn) -> None:
    fp = add_footprint(app_db)
    values = [50.0] * 5 + [1.0] * 90 + [1.0]
    for day in seed_days(app_db, fp, FIRST, values):
        write_day(app_db, day)
    row = current_day(app_db, FIRST + timedelta(days=95))
    assert row["baseline_days"] == 90
    assert row["baseline_median"] == pytest.approx(math.log(2))


def test_a_late_revision_of_an_older_day_does_not_rescore_a_later_one(app_db: Conn) -> None:
    fp = history(app_db, [0.0] * 90)
    seed_days(app_db, fp, TARGET, [12.0])
    write_day(app_db, TARGET)
    stored = current_day(app_db, TARGET)
    old = TARGET - timedelta(days=10)
    doc = add_document(app_db, 7001)
    add_detection(app_db, fp, old, 200.0, doc)
    assert write_day(app_db, old) is not None
    assert write_day(app_db, TARGET) is None
    assert current_day(app_db, TARGET) == stored
    assert current_day(app_db, old)["x"] == pytest.approx(math.log(201))


def test_nothing_is_written_without_both_satellites(app_db: Conn) -> None:
    fp = add_footprint(app_db)
    doc = add_document(app_db, 7002)
    add_coverage(app_db, FIRST, doc, satellites=("SNPP",))
    add_detection(app_db, fp, FIRST, 5.0, doc)
    assert write_day(app_db, FIRST) is None
    add_coverage(app_db, FIRST, doc, satellites=("NOAA20",))
    assert write_day(app_db, FIRST) is not None


def test_nothing_is_written_without_any_coverage(app_db: Conn) -> None:
    add_footprint(app_db)
    assert write_day(app_db, FIRST) is None


def test_a_known_figure_is_not_written_twice(app_db: Conn) -> None:
    fp = add_footprint(app_db)
    seed_days(app_db, fp, FIRST, [4.0])
    assert write_day(app_db, FIRST) is not None
    assert write_day(app_db, FIRST) is None
    count = app_db.execute("SELECT count(*) AS n FROM flare_day").fetchone()
    assert count == {"n": 1}


def test_a_new_detection_supersedes_and_as_of_still_sees_the_first(app_db: Conn) -> None:
    fp = add_footprint(app_db)
    seed_days(app_db, fp, FIRST, [4.0])
    first = write_day(app_db, FIRST)
    add_detection(app_db, fp, FIRST, 6.0, add_document(app_db, 7003), hour=9)
    second = write_day(app_db, FIRST)
    assert first is not None
    assert second is not None
    row = app_db.execute(
        "SELECT supersedes, n_detections, frp_sum_mw FROM flare_day WHERE id = %s", (second,)
    ).fetchone()
    assert row is not None
    assert (row["supersedes"], row["n_detections"], float(row["frp_sum_mw"])) == (first, 2, 10.0)
    stamp = app_db.execute("SELECT known_at FROM flare_day WHERE id = %s", (second,)).fetchone()
    assert stamp is not None
    at_first = app_db.execute(
        "SELECT id FROM flare_day_current_as_of(%s - interval '1 microsecond')",
        (stamp["known_at"],),
    ).fetchall()
    assert [r["id"] for r in at_first] == [first]


def test_sp_replaces_nrt_for_a_satellite(app_db: Conn) -> None:
    fp = add_footprint(app_db)
    doc = add_document(app_db, 7004)
    add_coverage(app_db, FIRST, doc, product="NRT")
    add_detection(app_db, fp, FIRST, 100.0, doc, satellite="SNPP", product="NRT")
    add_coverage(app_db, FIRST, doc, satellites=("SNPP",), product="SP")
    add_detection(app_db, fp, FIRST, 3.0, doc, satellite="SNPP", product="SP", minute=5)
    add_detection(app_db, fp, FIRST, 2.0, doc, satellite="NOAA20", product="NRT", minute=7)
    write_day(app_db, FIRST)
    row = current_day(app_db, FIRST)
    assert float(row["frp_sum_mw"]) == 5.0
    assert row["n_detections"] == 2
    assert row["products"] == ["NOAA20:NRT", "SNPP:SP"]


def test_only_detections_of_that_utc_day_count(app_db: Conn) -> None:
    fp = add_footprint(app_db)
    doc = add_document(app_db, 7005)
    add_coverage(app_db, FIRST, doc)
    add_detection(app_db, fp, FIRST, 2.0, doc, hour=23, minute=59)
    add_detection(app_db, fp, FIRST + timedelta(days=1), 50.0, doc, hour=0, minute=0)
    write_day(app_db, FIRST)
    assert float(current_day(app_db, FIRST)["frp_sum_mw"]) == 2.0


def test_a_live_row_is_available_when_known(app_db: Conn) -> None:
    fp = add_footprint(app_db)
    seed_days(app_db, fp, FIRST, [4.0])
    write_day(app_db, FIRST)
    row = current_day(app_db, FIRST)
    assert row["available_at"] == row["known_at"]
    assert row["backfilled"] is False


def test_a_backfilled_row_is_available_three_hours_after_the_last_detection(
    app_db: Conn,
) -> None:
    fp = add_footprint(app_db)
    doc = add_document(app_db, 7006)
    add_coverage(app_db, FIRST, doc)
    add_detection(app_db, fp, FIRST, 2.0, doc, hour=5, minute=10)
    add_detection(app_db, fp, FIRST, 3.0, doc, hour=14, minute=20)
    write_day(app_db, FIRST, backfilled=True)
    row = current_day(app_db, FIRST)
    assert row["backfilled"] is True
    assert row["available_at"] == datetime(2026, 1, 1, 17, 20, tzinfo=UTC)
    assert row["available_at"] <= row["known_at"]


def test_a_backfilled_day_without_detections_uses_the_end_of_the_day(app_db: Conn) -> None:
    fp = add_footprint(app_db)
    seed_days(app_db, fp, FIRST, [0.0])
    write_day(app_db, FIRST, backfilled=True)
    assert current_day(app_db, FIRST)["available_at"] == datetime(2026, 1, 2, 3, tzinfo=UTC)


def test_a_backfilled_row_is_never_available_after_it_is_known(app_db: Conn) -> None:
    fp = add_footprint(app_db)
    today = datetime.now(UTC).date()
    seed_days(app_db, fp, today, [0.0])
    write_day(app_db, today, backfilled=True)
    row = current_day(app_db, today)
    assert row["available_at"] <= row["known_at"]


def test_the_job_run_is_recorded(app_db: Conn) -> None:
    fp = add_footprint(app_db)
    seed_days(app_db, fp, FIRST, [4.0])
    job = app_db.execute("INSERT INTO job_run (job) VALUES ('fetch_nasa_firms') RETURNING id")
    run = job.fetchone()
    assert run is not None
    app_db.execute("SELECT flare_day_write('R9001', %s, false, %s)", (FIRST, run["id"]))
    assert current_day(app_db, FIRST)["job_run_id"] == run["id"]


@pytest.fixture
def private_app(seeded_schema: str, app_url: str) -> Iterator[Conn]:
    conn = open_in_schema(app_url, seeded_schema)
    yield conn
    conn.rollback()
    conn.close()


def test_score_write_days_goes_in_date_order_and_counts_rows(private_app: Conn) -> None:
    conn = private_app
    fp = add_footprint(conn)
    days = seed_days(conn, fp, FIRST, [1.0, 2.0, 3.0])
    run = conn.execute("INSERT INTO job_run (job) VALUES ('fetch_nasa_firms') RETURNING id")
    found = run.fetchone()
    assert found is not None
    written = write_days(conn, ["R9001", "R9002"], list(reversed(days)), False, found["id"])
    assert written == 3
    ids = conn.execute("SELECT day FROM flare_day ORDER BY id").fetchall()
    assert [r["day"] for r in ids] == days
    assert write_days(conn, ["R9001"], days, False, found["id"]) == 0


def test_a_revision_of_a_day_is_not_in_its_own_baseline(app_db: Conn) -> None:
    fp = history(app_db, [None] * 10 + [0.0] * 80)
    seed_days(app_db, fp, TARGET, [12.0])
    write_day(app_db, TARGET)
    first = current_day(app_db, TARGET)
    add_detection(app_db, fp, TARGET, 50.0, add_document(app_db, 7101), minute=30)
    assert write_day(app_db, TARGET) is not None
    second = current_day(app_db, TARGET)
    assert (first["baseline_days"], second["baseline_days"]) == (80, 80)


def test_sp_with_the_same_figures_still_writes_a_row(app_db: Conn) -> None:
    fp = add_footprint(app_db)
    doc = add_document(app_db, 7102)
    add_coverage(app_db, FIRST, doc, product="NRT")
    add_detection(app_db, fp, FIRST, 3.0, doc, satellite="SNPP", product="NRT")
    write_day(app_db, FIRST)
    add_coverage(app_db, FIRST, doc, satellites=("SNPP",), product="SP")
    add_detection(app_db, fp, FIRST, 3.0, doc, satellite="SNPP", product="SP", minute=5)
    assert write_day(app_db, FIRST) is not None
    assert current_day(app_db, FIRST)["products"] == ["NOAA20:NRT", "SNPP:SP"]
