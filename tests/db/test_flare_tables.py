import re
import secrets
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from tests.db.conftest import APP_URL, app_role_of, drop_schema, seed_schema
from tests.db.flare_helpers import (
    add_alert,
    add_attempt,
    add_day,
    add_detection_and_coverage,
    add_footprint,
)
from tests.db.helpers import insert_raw_outage, open_in_schema, sourced

from refinery_watch.db.connect import Conn
from refinery_watch.db.migrate import upgrade

pytestmark = pytest.mark.integration

TABLES = (
    "plant_footprint",
    "flare_detection",
    "flare_coverage",
    "flare_day",
    "flare_threshold",
    "flare_alert",
    "flare_alert_attempt",
)
D1 = date(2026, 11, 10)
STAMP = {
    "flare_alert": "built_at",
    "flare_alert_attempt": "attempted_at",
}
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "refinery_watch"
    / "db"
    / "migrations"
    / "versions"
    / "r0009_flare_signal.py"
)


def sqlstate(info: pytest.ExceptionInfo[psycopg.Error]) -> str | None:
    return info.value.sqlstate


def fill_every_table(conn: Conn) -> None:
    footprint = add_footprint(conn)
    add_detection_and_coverage(conn, footprint, D1)
    day = add_day(conn, footprint, D1)
    add_alert(conn, day, D1)
    alert = conn.execute("SELECT id FROM flare_alert").fetchone()
    assert alert is not None
    add_attempt(conn, int(alert["id"]))


@pytest.mark.parametrize("table", TABLES)
def test_app_role_cannot_update_delete_or_truncate(app_db: Conn, table: str) -> None:
    for statement in (
        f"UPDATE {table} SET {STAMP.get(table, 'known_at')} = now() WHERE false",
        f"DELETE FROM {table} WHERE false",
        f"TRUNCATE {table}",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as info, app_db.transaction():
            app_db.execute(statement)
        assert sqlstate(info) == "42501", statement


@pytest.mark.parametrize("table", TABLES)
def test_app_role_holds_select_and_insert_only(
    privileges: Callable[[str], set[str]], table: str
) -> None:
    assert privileges(table) == {"SELECT", "INSERT"}


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("verb", ["UPDATE {t} SET {c} = now()", "DELETE FROM {t}"])
def test_owner_is_stopped_by_trigger_on_update_and_delete(db: Conn, table: str, verb: str) -> None:
    fill_every_table(db)
    with pytest.raises(psycopg.Error) as info, db.transaction():
        db.execute(verb.format(t=table, c=STAMP.get(table, "known_at")))
    assert sqlstate(info) == "RW011"


def test_owner_is_stopped_by_trigger_on_truncate(db: Conn) -> None:
    fill_every_table(db)
    with pytest.raises(psycopg.Error) as info, db.transaction():
        db.execute(f"TRUNCATE {', '.join(TABLES)}")
    assert sqlstate(info) == "RW011"
    rows = db.execute(
        "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
        " WHERE t.tgname LIKE '%%_no_truncate' AND (t.tgtype & 32) = 32"
    ).fetchall()
    assert {r["relname"] for r in rows} >= set(TABLES)


def test_known_at_given_in_the_past_or_future_is_replaced_on_every_table(db: Conn) -> None:
    footprint = add_footprint(db, known_at=datetime(2001, 1, 1, tzinfo=UTC))
    day = add_day(db, footprint, D1, known_at=datetime(2999, 1, 1, tzinfo=UTC))
    add_alert(db, day, D1, built_at=datetime(2001, 1, 1, tzinfo=UTC))
    alert = db.execute("SELECT id FROM flare_alert").fetchone()
    assert alert is not None
    add_attempt(db, int(alert["id"]), attempted_at=datetime(2001, 1, 1, tzinfo=UTC))
    for table, column in (
        ("plant_footprint", "known_at"),
        ("flare_day", "known_at"),
        ("flare_alert", "built_at"),
        ("flare_alert_attempt", "attempted_at"),
    ):
        row = db.execute(
            f"SELECT abs(extract(epoch FROM {column} - clock_timestamp())) < 30 AS near"
            f" FROM {table}"
        ).fetchone()
        assert row == {"near": True}, table


def test_two_roots_for_one_refinery_and_day_fail(db: Conn) -> None:
    footprint = add_footprint(db)
    add_day(db, footprint, D1)
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        add_day(db, footprint, D1)
    add_day(db, footprint, D1 + timedelta(days=1))


def test_two_footprint_roots_for_one_refinery_fail(db: Conn) -> None:
    add_footprint(db)
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        add_footprint(db)
    add_footprint(db, "R9003")


def test_two_rows_superseding_one_row_fail(db: Conn) -> None:
    footprint = add_footprint(db)
    first = add_day(db, footprint, D1)
    add_day(db, footprint, D1, supersedes=first, n_detections=1)
    with pytest.raises(psycopg.Error) as info, db.transaction():
        add_day(db, footprint, D1, supersedes=first, n_detections=2)
    assert sqlstate(info) == "RW021"


def test_superseding_a_row_of_another_day_or_plant_fails_rw021(db: Conn) -> None:
    footprint = add_footprint(db)
    other = add_footprint(db, "R9003")
    first = add_day(db, footprint, D1)
    with pytest.raises(psycopg.Error) as info, db.transaction():
        add_day(db, footprint, D1 + timedelta(days=1), supersedes=first)
    assert sqlstate(info) == "RW021"
    with pytest.raises(psycopg.Error) as info, db.transaction():
        add_day(db, other, D1, refinery_id="R9003", supersedes=first)
    assert sqlstate(info) == "RW021"
    with pytest.raises(psycopg.Error) as info, db.transaction():
        add_footprint(db, "R9003", supersedes=footprint)
    assert sqlstate(info) == "RW021"


def test_a_revision_needs_a_later_known_at_rw022(db: Conn) -> None:
    db.execute("ALTER TABLE plant_footprint DISABLE TRIGGER plant_footprint_before_insert")
    first = add_footprint(db, known_at=datetime.now(UTC) + timedelta(hours=1))
    db.execute("ALTER TABLE plant_footprint ENABLE TRIGGER plant_footprint_before_insert")
    with pytest.raises(psycopg.Error) as info, db.transaction():
        add_footprint(db, supersedes=first, geometry_sha256="cd" * 32)
    assert sqlstate(info) == "RW022"


def test_flare_day_footprint_must_belong_to_the_refinery_rw023(db: Conn) -> None:
    other = add_footprint(db, "R9003")
    with pytest.raises(psycopg.Error) as info, db.transaction():
        add_day(db, other, D1)
    assert sqlstate(info) == "RW023"


def test_z_needs_sixty_baseline_days(db: Conn) -> None:
    footprint = add_footprint(db)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        add_day(db, footprint, D1, baseline_days=59, z=1.0)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        add_day(db, footprint, D1, baseline_days=60, z=None)
    add_day(db, footprint, D1, baseline_days=59, z=None)
    add_day(db, footprint, D1 + timedelta(days=1), baseline_days=60, z=1.0)


def test_live_row_available_at_must_equal_known_at(db: Conn) -> None:
    footprint = add_footprint(db)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        add_day(db, footprint, D1, available_at=datetime(2026, 1, 1, tzinfo=UTC))
    live = add_day(db, footprint, D1)
    row = db.execute("SELECT available_at = known_at AS same FROM flare_day WHERE id = %s", (live,))
    assert row.fetchone() == {"same": True}


def test_backfilled_row_available_at_may_be_earlier_but_not_later(db: Conn) -> None:
    footprint = add_footprint(db)
    early = datetime(2026, 1, 1, 3, tzinfo=UTC)
    add_day(db, footprint, D1, backfilled=True, available_at=early)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        add_day(
            db,
            footprint,
            D1 + timedelta(days=1),
            backfilled=True,
            available_at=datetime(2999, 1, 1, tzinfo=UTC),
        )


def test_second_alert_for_a_plant_on_one_dubai_day_fails(db: Conn) -> None:
    footprint = add_footprint(db)
    one = add_day(db, footprint, D1)
    two = add_day(db, footprint, D1 + timedelta(days=1))
    add_alert(db, one, D1)
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        add_alert(db, two, D1 + timedelta(days=1))


def test_second_alert_for_one_flare_date_fails_on_a_later_dubai_day(db: Conn) -> None:
    footprint = add_footprint(db)
    one = add_day(db, footprint, D1)
    add_alert(db, one, D1)
    db.execute("ALTER TABLE flare_alert DISABLE TRIGGER flare_alert_before_insert")
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        add_alert(
            db,
            one,
            D1,
            built_at=datetime.now(UTC) + timedelta(days=1),
            idempotency_key="f" * 32,
        )


def test_alert_day_is_the_dubai_date(db: Conn) -> None:
    footprint = add_footprint(db)
    day = add_day(db, footprint, D1)
    db.execute("ALTER TABLE flare_alert DISABLE TRIGGER flare_alert_before_insert")
    add_alert(db, day, D1, built_at=datetime(2026, 11, 12, 20, 30, tzinfo=UTC))
    row = db.execute("SELECT alert_day FROM flare_alert").fetchone()
    assert row == {"alert_day": date(2026, 11, 13)}


def test_alert_for_a_backfilled_or_superseded_day_fails_rw024(db: Conn) -> None:
    footprint = add_footprint(db)
    backfilled = add_day(
        db, footprint, D1, backfilled=True, available_at=datetime(2026, 11, 10, 3, tzinfo=UTC)
    )
    with pytest.raises(psycopg.Error) as info, db.transaction():
        add_alert(db, backfilled, D1)
    assert sqlstate(info) == "RW024"
    live = add_day(db, footprint, D1 + timedelta(days=1))
    add_day(db, footprint, D1 + timedelta(days=1), supersedes=live, n_detections=1)
    with pytest.raises(psycopg.Error) as info, db.transaction():
        add_alert(db, live, D1 + timedelta(days=1))
    assert sqlstate(info) == "RW024"


def test_alert_date_must_match_the_day_rw024(db: Conn) -> None:
    footprint = add_footprint(db)
    day = add_day(db, footprint, D1)
    with pytest.raises(psycopg.Error) as info, db.transaction():
        add_alert(db, day, D1 + timedelta(days=1))
    assert sqlstate(info) == "RW024"


def test_subject_with_a_line_break_is_refused(db: Conn) -> None:
    footprint = add_footprint(db)
    day = add_day(db, footprint, D1)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        add_alert(db, day, D1, subject="line one\r\nBcc: x")


def test_attempt_limits_rw025_and_checks(db: Conn) -> None:
    footprint = add_footprint(db)
    day = add_day(db, footprint, D1)
    alert = add_alert(db, day, D1)
    for n in (1, 2, 3):
        add_attempt(db, alert, n)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        add_attempt(db, alert, 4)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        add_attempt(db, alert, 1, status="sent")


def test_no_attempt_after_a_sent_one(db: Conn) -> None:
    footprint = add_footprint(db)
    day = add_day(db, footprint, D1)
    alert = add_alert(db, day, D1)
    add_attempt(db, alert, 1, status="sent", http_status=200, provider_message_id="m1", error=None)
    with pytest.raises(psycopg.Error) as info, db.transaction():
        add_attempt(db, alert, 2)
    assert sqlstate(info) == "RW025"


def test_no_attempt_after_eighteen_hours(db: Conn) -> None:
    footprint = add_footprint(db)
    day = add_day(db, footprint, D1)
    db.execute("ALTER TABLE flare_alert DISABLE TRIGGER flare_alert_before_insert")
    alert = add_alert(db, day, D1, built_at=datetime.now(UTC) - timedelta(hours=19))
    with pytest.raises(psycopg.Error) as info, db.transaction():
        add_attempt(db, alert, 1)
    assert sqlstate(info) == "RW025"


def test_an_attempt_inside_eighteen_hours_is_allowed(db: Conn) -> None:
    footprint = add_footprint(db)
    day = add_day(db, footprint, D1)
    db.execute("ALTER TABLE flare_alert DISABLE TRIGGER flare_alert_before_insert")
    alert = add_alert(db, day, D1, built_at=datetime.now(UTC) - timedelta(hours=17))
    add_attempt(db, alert, 1)


def test_two_default_thresholds_exist_and_apply_at_any_time(db: Conn) -> None:
    rows = db.execute(
        "SELECT kind, z_min, min_detections, method FROM flare_threshold ORDER BY kind"
    ).fetchall()
    assert rows == [
        {"kind": "alert", "z_min": Decimal("6.00"), "min_detections": 2, "method": "default"},
        {"kind": "signal", "z_min": Decimal("3.50"), "min_detections": 1, "method": "default"},
    ]
    old = db.execute(
        "SELECT z_min FROM flare_threshold_as_of('signal', '2001-01-01+00')"
    ).fetchone()
    assert old == {"z_min": Decimal("3.50")}


def test_current_as_of_functions_see_only_what_was_known(db: Conn) -> None:
    footprint = add_footprint(db)
    first = add_day(db, footprint, D1)
    stamp = db.execute("SELECT known_at FROM flare_day WHERE id = %s", (first,)).fetchone()
    assert stamp is not None
    second = add_day(db, footprint, D1, supersedes=first, n_detections=3)
    before = db.execute("SELECT id FROM flare_day_current_as_of(%s)", (stamp["known_at"],))
    assert [r["id"] for r in before.fetchall()] == [first]
    after = db.execute("SELECT id FROM flare_day_current_as_of(clock_timestamp())").fetchall()
    assert [r["id"] for r in after] == [second]
    none = db.execute("SELECT id FROM flare_day_current_as_of('2001-01-01+00')").fetchall()
    assert none == []
    fp_now = db.execute("SELECT id FROM footprint_current_as_of(clock_timestamp())").fetchall()
    assert [r["id"] for r in fp_now] == [footprint]


def test_the_migration_source_writes_no_outage_rows_and_no_edits() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert not re.search(r"INSERT\s+INTO\s+(outage_event|event_source)", source, re.IGNORECASE)
    assert not re.search(r"UPDATE\s+\w+\s+SET", source, re.IGNORECASE)
    assert not re.search(r"DELETE\s+FROM", source, re.IGNORECASE)


def test_upgrade_from_0008_leaves_outage_event_alone(pg_server: str) -> None:
    schema = f"rw_test_{secrets.token_hex(4)}"
    role = app_role_of(APP_URL)
    with psycopg.connect(pg_server, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        upgrade(pg_server, app_role=role, schema=schema, revision="0008")
        seed_schema(pg_server, schema)
        with open_in_schema(pg_server, schema) as conn:
            sourced(conn, insert_raw_outage(conn))
            conn.commit()
            before = conn.execute("SELECT * FROM outage_event ORDER BY id").fetchall()
            assert conn.execute("SELECT to_regclass('flare_day')").fetchone() == {
                "to_regclass": None
            }
        upgrade(pg_server, app_role=role, schema=schema, revision="0009")
        with open_in_schema(pg_server, schema) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            after = conn.execute("SELECT * FROM outage_event ORDER BY id").fetchall()
            count = conn.execute("SELECT count(*) AS n FROM flare_threshold").fetchone()
        assert version == {"version_num": "0009"}
        assert before == after
        assert len(after) == 1
        assert count == {"n": 2}
    finally:
        drop_schema(pg_server, schema)


def test_known_at_given_for_detection_coverage_and_threshold_is_replaced(db: Conn) -> None:
    footprint = add_footprint(db)
    add_detection_and_coverage(db, footprint, D1, known_at="2001-01-01+00")
    db.execute(
        "INSERT INTO flare_threshold (kind, z_min, min_detections, method, known_at)"
        " VALUES ('signal', 4, 1, 'calibrated', '2999-01-01+00')"
    )
    for table in ("flare_detection", "flare_coverage"):
        row = db.execute(
            f"SELECT abs(extract(epoch FROM known_at - clock_timestamp())) < 30 AS near"
            f" FROM {table}"
        ).fetchone()
        assert row == {"near": True}, table
    row = db.execute(
        "SELECT abs(extract(epoch FROM known_at - clock_timestamp())) < 30 AS near"
        " FROM flare_threshold WHERE method = 'calibrated'"
    ).fetchone()
    assert row == {"near": True}
