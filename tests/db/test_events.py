import threading
import time
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from tests.db.helpers import (
    add_document,
    day,
    open_in_schema,
    ts,
    unit_id,
    wait_for_lock_waiter,
)

from refinery_watch.db import events
from refinery_watch.db.connect import Conn
from refinery_watch.db.events import (
    Duplicate,
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
        "SELECT role, source_kind, known_at >= %s AS after_event FROM event_source"
        " WHERE outage_event_id = %s",
        (ts(1), written.id),
    ).fetchone()
    assert found == {"role": "primary", "source_kind": "company_statement", "after_event": True}


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
        ("refinery_change", "revision_changes_unit"),
        ("unit_change_closed", "revision_changes_unit"),
        ("early_restart", "restart_before_event_start"),
        ("late_revision", "revision_after_restart_date"),
        ("plant_restart", "plant_restart_units_open"),
        ("future_restart", "restart_in_future"),
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
        candidate = new_row(app_db, known_at=ts(20), start_date=day(20))
    elif scenario == "refinery_change":
        candidate = new_row(
            app_db, supersedes=e1.id, known_at=ts(3), refinery_id="R9003", refinery_unit_id=None
        )
    elif scenario == "unit_change_closed":
        must_write(app_db, restart_row(app_db, known_at=ts(5), start_date=day(5)), 3)
        candidate = new_row(
            app_db, supersedes=e1.id, known_at=ts(6), refinery_unit_id=None, whole_plant=True
        )
    elif scenario == "early_restart":
        x1 = must_write(app_db, restart_row(app_db, known_at=ts(5), start_date=day(5)), 3)
        candidate = restart_row(
            app_db, supersedes=x1.id, known_at=ts(6), start_date=date(2025, 10, 30)
        )
    elif scenario == "future_restart":
        candidate = restart_row(app_db, known_at=ts(5), start_date=day(20))
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


def test_write_event_refuses_a_repeatable_read_transaction(app_db: Conn) -> None:
    app_db.rollback()
    app_db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    with pytest.raises(RuntimeError, match="READ COMMITTED"):
        write_event(app_db, new_row(app_db), link(app_db, 1))


def test_connection_stays_usable_after_a_review(app_db: Conn) -> None:
    first = must_write(app_db, new_row(app_db), 1)
    review = write_event(
        app_db, new_row(app_db, known_at=ts(2), start_date=day(20)), link(app_db, 2)
    )
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


def test_late_report_of_a_closed_outage_is_a_duplicate_not_a_new_root(app_db: Conn) -> None:
    e1 = must_write(app_db, new_row(app_db), 1)
    must_write(app_db, restart_row(app_db, known_at=ts(5), start_date=day(5)), 2)
    result = write_event(app_db, new_row(app_db, known_at=ts(7)), link(app_db, 3))
    assert result == Duplicate(e1.chain_id, e1.id, corroboration_added=True)
    assert outage_as_of(app_db, ts(8)) == []
    sources = app_db.execute(
        "SELECT role FROM event_source WHERE outage_event_id = %s ORDER BY id", (e1.id,)
    ).fetchall()
    assert sources == [{"role": "primary"}, {"role": "corroboration"}]
    roots = app_db.execute(
        "SELECT count(*) AS n FROM outage_event WHERE event_type <> 'restart'"
    ).fetchone()
    assert roots == {"n": 1}


def test_report_inside_the_window_of_an_open_chain_is_a_duplicate(app_db: Conn) -> None:
    e1 = must_write(app_db, new_row(app_db), 1)
    revised = must_write(app_db, new_row(app_db, supersedes=e1.id, known_at=ts(2)), 2)
    result = write_event(
        app_db, new_row(app_db, known_at=ts(3), start_date=day(2)), link(app_db, 3)
    )
    assert result == Duplicate(chain_id=e1.chain_id, corroborated_event_id=revised.id)


def test_same_document_twice_is_still_a_duplicate(app_db: Conn) -> None:
    first_link = link(app_db, 1)
    e1 = write_event(app_db, new_row(app_db), first_link)
    assert isinstance(e1, Written)
    same = write_event(app_db, new_row(app_db, known_at=ts(2)), first_link)
    assert isinstance(same, Duplicate)
    assert same.chain_id == e1.chain_id
    assert not same.corroboration_added


def test_two_matching_chains_are_ambiguous(app_db: Conn) -> None:
    must_write(app_db, new_row(app_db), 1)
    must_write(
        app_db, new_row(app_db, refinery_unit_id=unit_id(app_db, code="VDU"), known_at=ts(2)), 2
    )
    unnamed = new_row(app_db, refinery_unit_id=None, known_at=ts(3), start_date=day(2))
    result = write_event(app_db, unnamed, link(app_db, 3))
    assert isinstance(result, Review)
    assert result.reason == "ambiguous_duplicate"


def test_revisions_and_restarts_skip_the_duplicate_check(app_db: Conn) -> None:
    e1 = must_write(app_db, new_row(app_db), 1)
    revised = write_event(
        app_db, new_row(app_db, supersedes=e1.id, known_at=ts(2)), link(app_db, 2)
    )
    assert isinstance(revised, Written)
    restarted = write_event(
        app_db, restart_row(app_db, known_at=ts(5), start_date=day(5)), link(app_db, 3)
    )
    assert isinstance(restarted, Written)


def test_a_distinct_outage_on_an_open_unit_still_goes_to_review(app_db: Conn) -> None:
    must_write(app_db, new_row(app_db), 1)
    result = write_event(
        app_db, new_row(app_db, known_at=ts(20), start_date=day(20)), link(app_db, 2)
    )
    assert isinstance(result, Review)
    assert result.reason == "unit_has_open_outage"


@pytest.mark.parametrize(
    ("restart", "over", "reason"),
    [
        (True, {"start_unknown": True, "confidence": "medium"}, "restart_date_unknown"),
        (True, {"start_unknown": True}, "restart_date_unknown"),
        (True, {"expected_restart_date": date(2025, 11, 9)}, "invalid_restart_fields"),
        (True, {"old_start": True, "confidence": "low"}, "invalid_restart_fields"),
        (False, {"start_unknown": True, "confidence": "high"}, "start_unknown_high_confidence"),
        (False, {"old_start": True, "confidence": "medium"}, "old_start_not_low"),
        (False, {"old_start": True, "confidence": "high"}, "old_start_not_low"),
        (False, {"whole_plant": True}, "whole_plant_has_unit"),
        (False, {"capacity_basis": "not_stated"}, "invalid_capacity"),
        (False, {"capacity_affected_kbd": None}, "invalid_capacity"),
        (False, {"capacity_affected_kbd": Decimal(0)}, "invalid_capacity"),
        (False, {"capacity_affected_kbd": Decimal("NaN")}, "invalid_capacity"),
        (False, {"capacity_affected_kbd": Decimal("Infinity")}, "invalid_capacity"),
        (False, {"match_score": 101}, "match_score_out_of_range"),
        (False, {"match_score": -1}, "match_score_out_of_range"),
    ],
)
def test_invalid_rows_return_a_review_and_write_nothing(
    app_db: Conn, restart: bool, over: dict[str, Any], reason: str
) -> None:
    make = restart_row if restart else new_row
    if restart:
        must_write(app_db, new_row(app_db), 1)
    before = app_db.execute("SELECT count(*) AS n FROM outage_event").fetchone()
    result = write_event(
        app_db, make(app_db, known_at=ts(5), start_date=day(5), **over), link(app_db, 9)
    )
    assert isinstance(result, Review)
    assert result.reason == reason
    assert result.detail
    after = app_db.execute("SELECT count(*) AS n FROM outage_event").fetchone()
    assert before == after


def test_valid_low_confidence_flags_are_written(app_db: Conn) -> None:
    unknown = new_row(app_db, start_unknown=True, confidence="low", known_at=ts(1))
    assert isinstance(write_event(app_db, unknown, link(app_db, 1)), Written)
    old = new_row(
        app_db,
        refinery_unit_id=unit_id(app_db, code="VDU"),
        old_start=True,
        confidence="low",
        known_at=ts(2),
        start_date=day(1),
    )
    assert isinstance(write_event(app_db, old, link(app_db, 2)), Written)


def test_held_lock_shows_up_as_an_error_not_a_hang(
    monkeypatch: pytest.MonkeyPatch, seeded_schema: str, app_url: str
) -> None:
    holder = open_in_schema(app_url, seeded_schema)
    writer = open_in_schema(app_url, seeded_schema)
    try:
        holder.execute("SELECT pg_advisory_xact_lock(7746001)")
        monkeypatch.setattr(events, "LOCK_TIMEOUT", "300ms")
        started = time.monotonic()
        with pytest.raises(psycopg.errors.LockNotAvailable):
            write_event(writer, new_row(writer), link(writer, 1))
        assert time.monotonic() - started < 5
        writer.rollback()
        assert writer.execute("SELECT 1 AS one").fetchone() == {"one": 1}
    finally:
        holder.close()
        writer.close()


def test_lock_timeout_is_restored_after_a_write(app_db: Conn) -> None:
    before = app_db.execute("SHOW lock_timeout").fetchone()
    must_write(app_db, new_row(app_db), 1)
    assert app_db.execute("SHOW lock_timeout").fetchone() == before


def test_no_merge_across_another_chains_restart(app_db: Conn) -> None:
    must_write(app_db, new_row(app_db), 1)
    must_write(app_db, restart_row(app_db, known_at=ts(8), start_date=day(8)), 2)
    e2 = must_write(app_db, new_row(app_db, known_at=ts(9), start_date=day(9)), 3)
    result = write_event(
        app_db, new_row(app_db, known_at=ts(10), start_date=day(6)), link(app_db, 4)
    )
    assert not (isinstance(result, Duplicate) and result.chain_id == e2.chain_id)


def test_late_report_of_e1_is_not_ambiguous_with_a_retrip(app_db: Conn) -> None:
    e1 = must_write(app_db, new_row(app_db), 1)
    must_write(app_db, restart_row(app_db, known_at=ts(2), start_date=day(2)), 2)
    must_write(app_db, new_row(app_db, known_at=ts(3), start_date=day(3)), 3)
    result = write_event(
        app_db, new_row(app_db, known_at=ts(5), start_date=day(1)), link(app_db, 4)
    )
    assert isinstance(result, Duplicate)
    assert result.chain_id == e1.chain_id


def test_second_report_of_a_restart_is_a_duplicate_not_a_review(app_db: Conn) -> None:
    must_write(app_db, new_row(app_db), 1)
    x1 = must_write(app_db, restart_row(app_db, known_at=ts(5), start_date=day(5)), 2)
    again = restart_row(app_db, known_at=ts(6), start_date=day(5))
    result = write_event(app_db, again, link(app_db, 3))
    assert result == Duplicate(x1.chain_id, x1.id, corroboration_added=True)
    count = app_db.execute("SELECT count(*) AS n FROM outage_event WHERE event_type = 'restart'")
    assert count.fetchone() == {"n": 1}


def test_integer_capacity_is_accepted(app_db: Conn) -> None:
    row = new_row(app_db, capacity_affected_kbd=200)
    assert isinstance(write_event(app_db, row, link(app_db, 1)), Written)


@pytest.mark.parametrize("bad", [200.0, "200", True, float("nan")])
def test_other_capacity_types_are_a_review_not_a_crash(app_db: Conn, bad: Any) -> None:
    result = write_event(app_db, new_row(app_db, capacity_affected_kbd=bad), link(app_db, 1))
    assert isinstance(result, Review)
    assert result.reason == "invalid_capacity"


def test_add_corroboration_takes_the_write_lock(
    monkeypatch: pytest.MonkeyPatch, seeded_schema: str, app_url: str
) -> None:
    setup = open_in_schema(app_url, seeded_schema)
    e1 = must_write(setup, new_row(setup, known_at=None), 1)
    setup.commit()
    holder = open_in_schema(app_url, seeded_schema)
    adder = open_in_schema(app_url, seeded_schema)
    try:
        holder.execute("SELECT pg_advisory_xact_lock(7746001)")
        monkeypatch.setattr(events, "LOCK_TIMEOUT", "300ms")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            add_corroboration(adder, e1.id, link(adder, 2))
    finally:
        for conn in (setup, holder, adder):
            conn.close()


@pytest.mark.parametrize("cdu_first", [True, False])
def test_concurrent_cdu_and_unnamed_reports_give_one_root_and_one_duplicate(
    seeded_schema: str, app_url: str, cdu_first: bool
) -> None:
    a = open_in_schema(app_url, seeded_schema)
    b = open_in_schema(app_url, seeded_schema)
    watcher = open_in_schema(app_url, seeded_schema)
    outcome: list[Written | Review | Duplicate] = []

    def named(conn: Conn) -> NewEventRow:
        return new_row(conn, known_at=None)

    def unnamed(conn: Conn) -> NewEventRow:
        return new_row(conn, known_at=None, refinery_unit_id=None)

    first, second = (named, unnamed) if cdu_first else (unnamed, named)

    def late_writer() -> None:
        outcome.append(write_event(b, second(b), link(b, 2)))
        b.commit()

    try:
        first_result = write_event(a, first(a), link(a, 1))
        thread = threading.Thread(target=late_writer)
        thread.start()
        wait_for_lock_waiter(watcher)
        assert thread.is_alive(), "the second writer did not wait for the lock"
        a.commit()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert isinstance(first_result, Written)
        assert isinstance(outcome[0], Duplicate)
        assert outcome[0].chain_id == first_result.chain_id
        roots = a.execute("SELECT count(*) AS n FROM outage_event").fetchone()
        assert roots == {"n": 1}
    finally:
        for conn in (a, b, watcher):
            conn.close()
