from collections.abc import Callable

import psycopg
import pytest
from tests.db.helpers import add_document, add_source, insert_outage, insert_raw_outage, ts

from refinery_watch.db.connect import Conn

pytestmark = pytest.mark.integration


def written_event(conn: Conn, n: int = 1) -> tuple[int, int]:
    """One outage with its primary source. Returns (outage id, source id)."""
    event = insert_outage(conn, known_at=ts(n))
    return event, add_source(conn, event, add_document(conn, n))


def sqlstate(info: pytest.ExceptionInfo[psycopg.Error]) -> str | None:
    return info.value.sqlstate


@pytest.mark.parametrize("table", ["outage_event", "event_source"])
def test_app_role_cannot_update_delete_or_truncate(app_db: Conn, table: str) -> None:
    column = "cause" if table == "outage_event" else "evidence"
    for statement in (
        f"UPDATE {table} SET {column} = {column}",
        f"DELETE FROM {table}",
        f"TRUNCATE {table}",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as info, app_db.transaction():
            app_db.execute(statement)
        assert sqlstate(info) == "42501", statement


def test_app_role_can_select_and_insert(app_db: Conn) -> None:
    written_event(app_db)
    rows = app_db.execute("SELECT count(*) AS n FROM outage_event").fetchone()
    sources = app_db.execute("SELECT count(*) AS n FROM event_source").fetchone()
    assert rows == {"n": 1}
    assert sources == {"n": 1}


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE outage_event SET cause = 'x'",
        "DELETE FROM outage_event",
        "TRUNCATE outage_event, event_source, review_item",
        "UPDATE event_source SET evidence = 'x'",
        "DELETE FROM event_source",
        "TRUNCATE event_source",
    ],
)
def test_owner_is_stopped_by_trigger(db: Conn, statement: str) -> None:
    written_event(db)
    db.execute("SET CONSTRAINTS ALL IMMEDIATE")
    with pytest.raises(psycopg.Error) as info, db.transaction():
        db.execute(statement)
    assert sqlstate(info) == "RW011"


def test_row_without_primary_source_fails_when_checked(db: Conn) -> None:
    with pytest.raises(psycopg.Error) as info, db.transaction():
        insert_outage(db)
        db.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert sqlstate(info) == "RW012"


def test_corroboration_alone_is_not_a_primary_source(db: Conn) -> None:
    with pytest.raises(psycopg.Error) as info, db.transaction():
        event = insert_outage(db)
        add_source(db, event, add_document(db), role="corroboration")
        db.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert sqlstate(info) == "RW012"


def test_second_primary_source_is_refused(db: Conn) -> None:
    event, _ = written_event(db)
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        add_source(db, event, add_document(db, 2))


def test_same_document_twice_is_refused(db: Conn) -> None:
    event, _ = written_event(db)
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        add_source(db, event, add_document(db, 1), role="corroboration")


def test_source_known_at_before_its_event_fails(db: Conn) -> None:
    event = insert_outage(db, known_at=ts(5))
    with pytest.raises(psycopg.Error) as info, db.transaction():
        add_source(db, event, add_document(db), known_at=ts(4))
    assert sqlstate(info) == "RW002"
    add_source(db, event, add_document(db), known_at=ts(5))


def test_source_known_at_defaults_to_clock(db: Conn) -> None:
    _, source = written_event(db)
    row = db.execute(
        "SELECT known_at > now() - interval '1 minute' AS recent FROM event_source WHERE id = %s",
        (source,),
    ).fetchone()
    assert row == {"recent": True}


def test_chain_and_supersedes_checks(db: Conn) -> None:
    event, _ = written_event(db)
    db.execute("SET CONSTRAINTS ALL IMMEDIATE")
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        insert_raw_outage(db, supersedes=event)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        insert_raw_outage(db, event_type="restart")


@pytest.mark.parametrize(
    "bad",
    [
        {"whole_plant": True},
        {"capacity_basis": "not_stated"},
        {"capacity_affected_kbd": None},
        {"start_unknown": True},
        {"old_start": True},
        {"match_score": 101},
        {"confidence": "certain"},
        {"capacity_affected_kbd": 0},
    ],
)
def test_row_checks(db: Conn, bad: dict[str, object]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        insert_outage(db, **bad)


def test_flag_combinations_that_are_allowed(db: Conn) -> None:
    variants: list[dict[str, object]] = [
        {"refinery_unit_id": None, "whole_plant": True},
        {"start_unknown": True, "confidence": "medium"},
        {"old_start": True, "confidence": "low"},
        {"capacity_basis": "not_stated", "capacity_affected_kbd": None},
    ]
    for n, variant in enumerate(variants, start=1):
        event = insert_outage(db, known_at=ts(n), **variant)
        add_source(db, event, add_document(db, n))


def test_unit_must_belong_to_the_refinery(db: Conn) -> None:
    other = db.execute(
        "SELECT id FROM refinery_unit WHERE refinery_id = 'R9002' LIMIT 1"
    ).fetchone()
    assert other is not None
    with pytest.raises(psycopg.errors.ForeignKeyViolation), db.transaction():
        insert_outage(db, refinery_unit_id=other["id"])


def test_event_source_evidence_not_empty(db: Conn) -> None:
    event, _ = written_event(db)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute(
            "INSERT INTO event_source (outage_event_id, raw_document_id, role, source_kind,"
            " evidence) VALUES (%s, %s, 'corroboration', 'regulator', '')",
            (event, add_document(db, 2)),
        )


def test_app_grants_match_table(privileges: Callable[[str], set[str]]) -> None:
    assert privileges("outage_event") == {"SELECT", "INSERT"}
    assert privileges("event_source") == {"SELECT", "INSERT"}


def test_public_has_no_execute_on_trigger_functions(db: Conn) -> None:
    rows = db.execute(
        "SELECT p.proname, has_function_privilege('public', p.oid, 'EXECUTE') AS e"
        " FROM pg_proc p WHERE p.pronamespace = current_schema()::regnamespace"
    ).fetchall()
    assert rows
    assert not [r["proname"] for r in rows if r["e"]]
