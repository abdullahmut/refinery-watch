"""tceq_event and price take known_at from the database clock and are append-only."""

import itertools
import secrets
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest
from tests.db.conftest import APP_URL, app_role_of, drop_schema, seed_schema
from tests.db.helpers import add_document, open_in_schema

from refinery_watch.db.connect import Conn
from refinery_watch.db.migrate import upgrade
from refinery_watch.flare.support import evidence_as_of

pytestmark = pytest.mark.integration

PAST = datetime(2001, 1, 1, tzinfo=UTC)
FUTURE = datetime(2999, 1, 1, tzinfo=UTC)
DAY = date(2026, 10, 12)
_docs = itertools.count(700)


def add_tceq(
    conn: Conn, known_at: datetime | None = None, started: datetime | None = None
) -> datetime:
    row = conn.execute(
        "INSERT INTO tceq_event (incident_id, report_stage, raw_document_id, refinery_id,"
        " regulated_entity, event_kind, started_at, report_url, known_at)"
        " VALUES (%s, 'initial', %s, 'R9001', 'Plant', 'Emissions event', %s, 'https://x.test',"
        " coalesce(%s, clock_timestamp())) RETURNING known_at",
        (f"T{next(_docs)}", add_document(conn, next(_docs)), started, known_at),
    ).fetchone()
    assert row is not None
    return row["known_at"]  # type: ignore[no-any-return]


def add_price(conn: Conn, known_at: datetime | None = None) -> datetime:
    row = conn.execute(
        "INSERT INTO price (series, observed_on, value, unit, source_id, known_at)"
        " VALUES ('eia.util.us', '2025-11-07', 80, 'pct', 'fixture_src',"
        " coalesce(%s, clock_timestamp())) RETURNING known_at",
        (known_at,),
    ).fetchone()
    assert row is not None
    return row["known_at"]  # type: ignore[no-any-return]


WRITERS = {"tceq_event": add_tceq, "price": add_price}


def clock(conn: Conn) -> datetime:
    row = conn.execute("SELECT clock_timestamp() AS t").fetchone()
    assert row is not None
    return row["t"]  # type: ignore[no-any-return]


@pytest.mark.parametrize("given", [PAST, FUTURE], ids=["past", "future"])
@pytest.mark.parametrize("table", WRITERS)
@pytest.mark.parametrize("role", ["owner", "app"])
def test_a_given_known_at_is_replaced_by_the_clock(
    db: Conn, app_db: Conn, role: str, table: str, given: datetime
) -> None:
    conn = db if role == "owner" else app_db
    stamped = WRITERS[table](conn, given)
    assert abs(stamped - clock(conn)) < timedelta(seconds=30)


def test_a_backdated_regulator_report_is_not_evidence_before_it_was_known(db: Conn) -> None:
    cut = clock(db)
    stamped = add_tceq(
        db, known_at=cut - timedelta(days=1), started=datetime(2026, 10, 11, 5, tzinfo=UTC)
    )
    assert stamped > cut
    assert [i.kind for i in evidence_as_of(db, "R9001", DAY, cut)] == []
    assert [i.kind for i in evidence_as_of(db, "R9001", DAY, stamped)] == ["regulator_near"]


@pytest.mark.parametrize("table", WRITERS)
@pytest.mark.parametrize(
    "verb", ["UPDATE {t} SET known_at = known_at", "DELETE FROM {t}", "TRUNCATE {t}"]
)
def test_owner_is_stopped_by_trigger(db: Conn, table: str, verb: str) -> None:
    WRITERS[table](db)
    with pytest.raises(psycopg.Error) as info, db.transaction():
        db.execute(verb.format(t=table))
    assert info.value.sqlstate == "RW011"


@pytest.mark.parametrize("table", WRITERS)
@pytest.mark.parametrize(
    "verb", ["UPDATE {t} SET known_at = known_at", "DELETE FROM {t}", "TRUNCATE {t}"]
)
def test_app_role_holds_no_edit_privilege(app_db: Conn, table: str, verb: str) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege), app_db.transaction():
        app_db.execute(verb.format(t=table))


def test_public_cannot_execute_the_stamp_function(db: Conn) -> None:
    row = db.execute(
        "SELECT has_function_privilege('public', 'stamp_known_at()', 'EXECUTE') AS p"
    ).fetchone()
    assert row == {"p": False}


def test_upgrade_from_0009_leaves_existing_rows_alone(pg_server: str) -> None:
    schema = f"rw_test_{secrets.token_hex(4)}"
    role = app_role_of(APP_URL)
    with psycopg.connect(pg_server, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        upgrade(pg_server, app_role=role, schema=schema, revision="0009")
        seed_schema(pg_server, schema)
        with open_in_schema(pg_server, schema) as conn:
            add_tceq(conn, known_at=PAST)
            add_price(conn, known_at=PAST)
            conn.commit()
            before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY id").fetchall() for t in WRITERS}
        upgrade(pg_server, app_role=role, schema=schema, revision="0010")
        with open_in_schema(pg_server, schema) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            after = {t: conn.execute(f"SELECT * FROM {t} ORDER BY id").fetchall() for t in WRITERS}
        assert version == {"version_num": "0010"}
        assert before == after
        assert [r["known_at"] for r in after["tceq_event"]] == [PAST]
    finally:
        drop_schema(pg_server, schema)
