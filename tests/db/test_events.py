from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from tests.db.helpers import add_document, day, ts, unit_id

from refinery_watch.db import events
from refinery_watch.db.connect import Conn
from refinery_watch.db.events import (
    NewEventRow,
    OutageRow,
    Review,
    SourceLink,
    Written,
    add_corroboration,
    match_restart,
    outage_as_of,
    outage_duplicate_of,
    restart_duplicate_of,
    write_event,
)

pytestmark = pytest.mark.integration


def new_row(conn: Conn, **over: Any) -> NewEventRow:
    fields: dict[str, Any] = {
        "event_type": "unplanned",
        "refinery_id": "R9001",
        "refinery_unit_id": unit_id(conn),
        "whole_plant": False,
        "start_date": day(1),
        "confidence": "high",
        "capacity_basis": "stated",
        "match_score": 100,
        "capacity_affected_kbd": Decimal("200"),
        "known_at": ts(1),
    }
    fields.update(over)
    return NewEventRow(**fields)


def link(conn: Conn, n: int) -> SourceLink:
    return SourceLink(add_document(conn, n), "company_statement", "quoted text")


def restart_row(conn: Conn, **over: Any) -> NewEventRow:
    base: dict[str, Any] = {
        "event_type": "restart",
        "capacity_basis": "not_stated",
        "capacity_affected_kbd": None,
    }
    base.update(over)
    return new_row(conn, **base)


def must_write(conn: Conn, row: NewEventRow, n: int) -> Written:
    result = write_event(conn, row, link(conn, n))
    assert isinstance(result, Written), result
    return result


def test_write_returns_ids_and_stores_the_primary_source(app_db: Conn) -> None:
    written = must_write(app_db, new_row(app_db), 1)
    assert written.chain_id == written.id
    assert written.ends_event_id is None
    found = app_db.execute(
        "SELECT role, source_kind, known_at FROM event_source WHERE outage_event_id = %s",
        (written.id,),
    ).fetchone()
    assert found == {"role": "primary", "source_kind": "company_statement", "known_at": ts(1)}


def test_restart_gets_ends_event_id_and_revision_gets_chain_id(app_db: Conn) -> None:
    e1 = must_write(app_db, new_row(app_db), 1)
    x1 = must_write(app_db, restart_row(app_db, known_at=ts(5), start_date=day(5)), 2)
    assert x1.ends_event_id == e1.id
    assert x1.chain_id == x1.id
    e2 = must_write(app_db, new_row(app_db, supersedes=e1.id, known_at=ts(6)), 3)
    assert e2.chain_id == e1.id


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("not_current", "supersedes_not_current"),
        ("family", "family_mismatch"),
        ("no_match", "no_restart_match"),
        ("occupied", "unit_has_open_outage"),
        ("unit_change", "revision_changes_unit"),
        ("early_restart", "restart_before_event_start"),
        ("late_revision", "revision_after_restart_date"),
        ("plant_restart", "plant_restart_units_open"),
    ],
)
def test_review_reason_for_each_rule(app_db: Conn, scenario: str, reason: str) -> None:
    vdu = unit_id(app_db, code="VDU")
    plant: dict[str, Any] = {"refinery_unit_id": None, "whole_plant": True}
    e1 = must_write(app_db, new_row(app_db), 1)
    must_write(app_db, new_row(app_db, refinery_unit_id=vdu, known_at=ts(2)), 2)
    candidate: NewEventRow
    if scenario == "not_current":
        must_write(app_db, new_row(app_db, supersedes=e1.id, known_at=ts(3)), 3)
        candidate = new_row(app_db, supersedes=e1.id, known_at=ts(4))
    elif scenario == "family":
        candidate = restart_row(app_db, supersedes=e1.id, known_at=ts(3))
    elif scenario == "no_match":
        candidate = restart_row(app_db, refinery_id="R9003", refinery_unit_id=None, known_at=ts(3))
    elif scenario == "occupied":
        candidate = new_row(app_db, known_at=ts(3))
    elif scenario == "unit_change":
        candidate = new_row(
            app_db, supersedes=e1.id, known_at=ts(3), refinery_id="R9003", refinery_unit_id=None
        )
    elif scenario == "early_restart":
        x1 = must_write(app_db, restart_row(app_db, known_at=ts(5), start_date=day(5)), 3)
        candidate = restart_row(
            app_db, supersedes=x1.id, known_at=ts(6), start_date=date(2025, 10, 30)
        )
    elif scenario == "late_revision":
        must_write(app_db, restart_row(app_db, known_at=ts(5), start_date=day(5)), 3)
        candidate = new_row(app_db, supersedes=e1.id, known_at=ts(6), start_date=day(9))
    else:
        candidate = restart_row(app_db, known_at=ts(5), start_date=day(5), **plant)
    result = write_event(app_db, candidate, link(app_db, 90))
    assert isinstance(result, Review)
    assert result.reason == reason
    assert result.detail


def test_ends_event_mismatch_maps_to_a_review(app_db: Conn) -> None:
    must_write(app_db, new_row(app_db), 1)
    other = must_write(
        app_db, new_row(app_db, refinery_unit_id=unit_id(app_db, code="VDU"), known_at=ts(2)), 2
    )
    with pytest.raises(psycopg.Error) as info, app_db.transaction():
        app_db.execute(
            "INSERT INTO outage_event (event_type, refinery_id, refinery_unit_id,"
            " capacity_basis, start_date, confidence, match_score, known_at, ends_event_id)"
            " VALUES ('restart', 'R9001', %s, 'not_stated', %s, 'high', 100, %s, %s)",
            (unit_id(app_db), day(5), ts(5), other.id),
        )
    review = events.review_from(info.value)
    assert review is not None
    assert review.reason == "ends_event_mismatch"


def test_unrelated_error_is_not_a_review() -> None:
    assert events.review_from(psycopg.errors.CheckViolation("x")) is None


def test_unknown_error_is_raised_not_returned(app_db: Conn) -> None:
    with pytest.raises(psycopg.Error) as info:
        write_event(
            app_db, new_row(app_db, known_at=datetime(2999, 1, 1, tzinfo=UTC)), link(app_db, 1)
        )
    assert info.value.sqlstate == "RW002"


def test_rw001_is_raised(app_db: Conn) -> None:
    app_db.rollback()
    app_db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    with pytest.raises(psycopg.Error) as info:
        write_event(app_db, new_row(app_db), link(app_db, 1))
    assert info.value.sqlstate == "RW001"


def test_connection_stays_usable_after_a_review(app_db: Conn) -> None:
    first = must_write(app_db, new_row(app_db), 1)
    review = write_event(app_db, new_row(app_db, known_at=ts(2)), link(app_db, 2))
    assert isinstance(review, Review)
    again = must_write(
        app_db, new_row(app_db, known_at=ts(3), refinery_unit_id=None, whole_plant=True), 3
    )
    assert again.id > first.id
    assert [r.id for r in outage_as_of(app_db, ts(4))] == [first.id, again.id]


def test_naive_datetimes_raise_value_error(app_db: Conn) -> None:
    naive = datetime(2025, 11, 1, 6)  # noqa: DTZ001
    with pytest.raises(ValueError, match="known_at"):
        write_event(app_db, new_row(app_db, known_at=naive), link(app_db, 1))
    with pytest.raises(ValueError, match="as_of"):
        outage_as_of(app_db, naive)
    with pytest.raises(ValueError, match="as_of"):
        match_restart(app_db, "R9001", None, day(1), naive)
    with pytest.raises(ValueError, match="as_of"):
        outage_duplicate_of(app_db, "R9001", None, False, day(1), False, naive)
    with pytest.raises(ValueError, match="as_of"):
        restart_duplicate_of(app_db, "R9001", None, day(1), naive)


def test_outage_as_of_returns_typed_rows(app_db: Conn) -> None:
    e1 = must_write(app_db, new_row(app_db, cause="fire", unit_label="CDU 1"), 1)
    rows = outage_as_of(app_db, ts(2))
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, OutageRow)
    assert row.id == e1.id
    assert row.capacity_affected_kbd == Decimal("200.0")
    assert (row.cause, row.unit_label, row.event_type) == ("fire", "CDU 1", "unplanned")
    assert row.known_at == ts(1)


def test_lookups_wrap_the_sql_functions(app_db: Conn) -> None:
    unit = unit_id(app_db)
    e1 = must_write(app_db, new_row(app_db), 1)
    assert match_restart(app_db, "R9001", unit, day(5), ts(2)) == e1.id
    assert match_restart(app_db, "R9001", None, day(5), ts(2)) is None
    assert outage_duplicate_of(app_db, "R9001", unit, False, day(2), False, ts(2)) == [e1.id]
    assert restart_duplicate_of(app_db, "R9001", unit, day(5), ts(2)) is None
    x1 = must_write(app_db, restart_row(app_db, known_at=ts(5), start_date=day(5)), 2)
    assert restart_duplicate_of(app_db, "R9001", unit, day(6), ts(6)) == x1.id


def test_corroboration_is_a_second_source(app_db: Conn) -> None:
    e1 = must_write(app_db, new_row(app_db), 1)
    second = link(app_db, 2)
    source_id = add_corroboration(app_db, e1.id, second)
    found = app_db.execute("SELECT role FROM event_source WHERE id = %s", (source_id,)).fetchone()
    assert found == {"role": "corroboration"}
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_corroboration(app_db, e1.id, replace(second, source_kind="regulator"))
