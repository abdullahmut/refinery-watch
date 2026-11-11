from collections.abc import Iterator

import psycopg
import pytest
from tests.db.helpers import open_in_schema

from refinery_watch.db.connect import Conn
from refinery_watch.sources.store import (
    RunOutcome,
    SourceMissing,
    SourceNotCleared,
    finish_run,
    source_row,
    start_run,
)

pytestmark = pytest.mark.integration

JOB = "fetch_hp_news"


@pytest.fixture
def app(seeded_schema: str, app_url: str) -> Iterator[Conn]:
    conn = open_in_schema(app_url, seeded_schema)
    yield conn
    conn.rollback()
    conn.close()


def add_run(conn: Conn, status: str, minutes_ago: int, error: str | None = None) -> int:
    finished = "NULL" if status == "running" else "now()"
    row = conn.execute(
        "INSERT INTO job_run (job, status, started_at, finished_at, error)"
        f" VALUES (%s, %s, clock_timestamp() - make_interval(mins => %s), {finished}, %s)"
        " RETURNING id",
        (JOB, status, minutes_ago, error),
    ).fetchone()
    assert row is not None
    conn.commit()
    return int(row["id"])


def runs(conn: Conn) -> list[tuple[str, str | None]]:
    rows = conn.execute("SELECT status, error FROM job_run WHERE job = %s ORDER BY id", (JOB,))
    return [(r["status"], r["error"]) for r in rows.fetchall()]


def test_first_run_starts(app: Conn) -> None:
    run_id = start_run(app, JOB, 120)
    assert run_id is not None
    assert runs(app) == [("running", None)]


def test_second_call_while_running_is_skipped(app: Conn) -> None:
    assert start_run(app, JOB, 120) is not None
    assert start_run(app, JOB, 120) is None
    assert runs(app) == [("running", None), ("skipped", "already running")]


def test_inside_the_interval_is_skipped(app: Conn) -> None:
    add_run(app, "ok", 100)
    assert start_run(app, JOB, 120) is None
    assert runs(app)[-1] == ("skipped", "interval")


def test_after_the_interval_starts(app: Conn) -> None:
    add_run(app, "ok", 110)
    assert start_run(app, JOB, 120) is not None


def test_failed_run_also_holds_the_interval(app: Conn) -> None:
    add_run(app, "failed", 10)
    assert start_run(app, JOB, 120) is None


def test_skipped_rows_do_not_hold_the_interval(app: Conn) -> None:
    add_run(app, "skipped", 1, "interval")
    assert start_run(app, JOB, 120) is not None


def test_stale_running_row_is_failed_and_the_new_run_starts(app: Conn) -> None:
    add_run(app, "running", 41)
    assert start_run(app, JOB, 1) is not None
    assert runs(app)[0] == ("failed", "stale: no finish recorded")
    assert runs(app)[-1] == ("running", None)


def test_fresh_running_row_is_not_touched(app: Conn) -> None:
    add_run(app, "running", 10)
    assert start_run(app, JOB, 1) is None
    assert runs(app)[0] == ("running", None)


def test_other_jobs_are_independent(app: Conn) -> None:
    assert start_run(app, JOB, 120) is not None
    assert start_run(app, "fetch_gdelt_doc", 60) is not None


def test_finish_run_records_counts_and_note(app: Conn) -> None:
    run_id = start_run(app, JOB, 120)
    assert run_id is not None
    finish_run(app, run_id, RunOutcome("ok", 3, 2, "one skipped"))
    row = app.execute("SELECT * FROM job_run WHERE id = %s", (run_id,)).fetchone()
    assert row is not None
    assert (row["status"], row["rows_in"], row["rows_out"], row["error"]) == (
        "ok",
        3,
        2,
        "one skipped",
    )
    assert row["finished_at"] is not None


def test_finish_run_after_a_failed_statement(app: Conn) -> None:
    run_id = start_run(app, JOB, 120)
    assert run_id is not None
    with pytest.raises(psycopg.errors.DivisionByZero):
        app.execute("SELECT 1/0")
    finish_run(app, run_id, RunOutcome("failed", 0, 0, "boom"))
    row = app.execute("SELECT status FROM job_run WHERE id = %s", (run_id,)).fetchone()
    assert row is not None
    assert row["status"] == "failed"


def test_finish_run_cuts_a_long_note(app: Conn) -> None:
    run_id = start_run(app, JOB, 120)
    assert run_id is not None
    finish_run(app, run_id, RunOutcome("failed", 0, 0, "x" * 5000))
    row = app.execute("SELECT error FROM job_run WHERE id = %s", (run_id,)).fetchone()
    assert row is not None
    assert len(row["error"]) == 1000


def test_source_row_reads_the_table(app: Conn) -> None:
    row = source_row(app, "hp_news")
    assert row.feed_url == "https://hp.example.com/rss?feed=news"
    assert row.fetch_interval_min == 120


def test_missing_source_raises_with_the_message(app: Conn) -> None:
    with pytest.raises(SourceMissing, match="source nope not loaded; run the source seed loader"):
        source_row(app, "nope")


@pytest.mark.parametrize(
    ("status", "cleared"),
    [
        ("unchecked", False),
        ("robots_refused_fetcher_must_verify", False),
        ("client_error_to_fetcher", False),
        ("unchecked_binary_robots_response", False),
        ("open", True),
        ("allowed_for_reference_use_ai_training_disallowed", True),
    ],
)
def test_source_robots_status_gates_the_run(db: Conn, status: str, cleared: bool) -> None:
    db.execute(
        "INSERT INTO source (id, name, tier, region_focus, access_method, feed_url,"
        " article_url_pattern, robots_status, fetch_interval_min)"
        " VALUES ('gate_src', 'Gate', 2, 'Global', 'rss', 'https://g.example.com/f',"
        " 'https://g.example.com/<id>', %s, 60)",
        (status,),
    )
    if cleared:
        assert source_row(db, "gate_src").robots_status == status
    else:
        with pytest.raises(SourceNotCleared):
            source_row(db, "gate_src")
