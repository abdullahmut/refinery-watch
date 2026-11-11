"""A cut-off from outage_cutoff() sees every row that can ever have known_at at or before it."""

import threading
from datetime import datetime
from decimal import Decimal

import pytest
from tests.db.helpers import add_document, open_in_schema, unit_id, wait_for_lock_waiter

from refinery_watch.db import events
from refinery_watch.db.connect import Conn
from refinery_watch.db.events import NewEventRow, SourceLink, add_corroboration, write_event

pytestmark = pytest.mark.integration


def cdu_row(conn: Conn, code: str = "CDU") -> NewEventRow:
    return NewEventRow(
        "unplanned",
        "R9001",
        unit_id(conn, code=code),
        False,
        datetime(2025, 11, 1).date(),  # noqa: DTZ001
        "high",
        "stated",
        100,
        capacity_affected_kbd=Decimal(200),
    )


def link(conn: Conn, n: int) -> SourceLink:
    return SourceLink(add_document(conn, n), "company_statement", "quoted text")


def count_open(conn: Conn, as_of: datetime) -> int:
    row = conn.execute("SELECT count(*) AS n FROM outage_as_of(%s)", (as_of,)).fetchone()
    assert row is not None
    return int(row["n"])


def read_cutoff(conn: Conn, out: list[datetime]) -> None:
    row = conn.execute("SELECT outage_cutoff() AS t").fetchone()
    assert row is not None
    out.append(row["t"])


def test_cutoff_waits_for_an_uncommitted_writer_and_then_stays_stable(
    seeded_schema: str, app_url: str
) -> None:
    writer = open_in_schema(app_url, seeded_schema)
    reader = open_in_schema(app_url, seeded_schema)
    reader.autocommit = True
    watcher = open_in_schema(app_url, seeded_schema)
    result: list[datetime] = []
    try:
        first = write_event(writer, cdu_row(writer), link(writer, 1))
        assert not isinstance(first, (events.Review, events.Duplicate))
        thread = threading.Thread(target=read_cutoff, args=(reader, result))
        thread.start()
        wait_for_lock_waiter(watcher)
        assert thread.is_alive(), "cutoff did not wait for the writer holding the lock"
        writer.commit()
        thread.join(timeout=5)
        assert not thread.is_alive()
        cutoff = result[0]
        known = reader.execute("SELECT max(known_at) AS k FROM outage_event").fetchone()
        assert known is not None
        assert cutoff >= known["k"]
        before = count_open(reader, cutoff)
        write_event(writer, cdu_row(writer, "VDU"), link(writer, 2))
        writer.commit()
        assert count_open(reader, cutoff) == before == 1
    finally:
        for conn in (writer, reader, watcher):
            conn.close()


def test_cutoff_covers_a_corroboration_still_uncommitted(seeded_schema: str, app_url: str) -> None:
    setup = open_in_schema(app_url, seeded_schema)
    first = write_event(setup, cdu_row(setup), link(setup, 1))
    assert not isinstance(first, (events.Review, events.Duplicate))
    setup.commit()
    writer = open_in_schema(app_url, seeded_schema)
    reader = open_in_schema(app_url, seeded_schema)
    reader.autocommit = True
    watcher = open_in_schema(app_url, seeded_schema)
    result: list[datetime] = []
    try:
        add_corroboration(writer, first.id, link(writer, 2))
        thread = threading.Thread(target=read_cutoff, args=(reader, result))
        thread.start()
        wait_for_lock_waiter(watcher)
        writer.commit()
        thread.join(timeout=5)
        query = "SELECT count(*) AS n FROM event_source WHERE known_at <= %s"
        before = reader.execute(query, (result[0],)).fetchone()
        after = reader.execute(query, (result[0],)).fetchone()
        assert before == after == {"n": 2}
    finally:
        for conn in (setup, writer, reader, watcher):
            conn.close()


def test_cutoff_inside_a_transaction_does_not_hold_the_lock(
    monkeypatch: pytest.MonkeyPatch, seeded_schema: str, app_url: str
) -> None:
    note = open_in_schema(app_url, seeded_schema)
    ingest = open_in_schema(app_url, seeded_schema)
    try:
        note.execute("SELECT outage_cutoff()")
        held = note.execute(
            "SELECT count(*) AS n FROM pg_locks"
            " WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
        ).fetchone()
        assert held == {"n": 0}
        monkeypatch.setattr(events, "LOCK_TIMEOUT", "1s")
        result = write_event(ingest, cdu_row(ingest), link(ingest, 1))
        assert isinstance(result, events.Written)
    finally:
        note.close()
        ingest.close()


def test_cutoff_is_granted_to_the_app_role_not_public(app_db: Conn, db: Conn) -> None:
    row = app_db.execute("SELECT outage_cutoff() AS t").fetchone()
    assert row is not None
    public = db.execute(
        "SELECT has_function_privilege('public', 'outage_cutoff()', 'EXECUTE') AS p"
    ).fetchone()
    assert public == {"p": False}
