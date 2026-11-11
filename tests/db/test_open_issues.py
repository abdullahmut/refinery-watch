"""Known defects. Each test states the correct behaviour and is expected to fail until fixed."""

import threading
from datetime import UTC, datetime

import pytest
from tests.db.helpers import current_ids, open_in_schema, ts
from tests.db.support import link, outage, restart

from refinery_watch.db.connect import Conn
from refinery_watch.db.events import (
    Duplicate,
    Review,
    Written,
    take_write_lock,
    write_event,
)

pytestmark = pytest.mark.integration

KEY = 7746001


@pytest.mark.xfail(
    strict=True,
    reason="open issue: RW015 needs the source publication date in its own timezone; the "
    "one-day UTC slack lets a restart dated tomorrow close today, and US evening reports "
    "run two local days ahead",
)
def test_rw015_utc_source_cannot_restart_tomorrow(db: Conn) -> None:
    write_event(db, outage(db, 1, 1), link(db, 30))
    known = datetime(2025, 11, 12, 10, 0, tzinfo=UTC)
    result = write_event(db, restart(db, 13, 1, known_at=known), link(db, 31))
    assert isinstance(result, Review), f"restart dated tomorrow closed the event today: {result}"


@pytest.mark.xfail(
    strict=True,
    reason="open issue: the restart lookup has no date window, so a restart dated before a "
    "re-trip is absorbed into the older restart",
)
def test_restart_dated_before_retrip_is_not_silently_absorbed(db: Conn) -> None:
    write_event(db, outage(db, 1, 1), link(db, 50))
    write_event(db, restart(db, 5, 5), link(db, 51))
    second = write_event(db, outage(db, 10, 10), link(db, 52))
    assert isinstance(second, Written), second
    result = write_event(db, restart(db, 9, 12), link(db, 53))
    assert not isinstance(result, Duplicate), f"absorbed into the old restart: {result}"
    assert current_ids(db, ts(13)) == [] or isinstance(result, Review)


@pytest.mark.xfail(
    strict=True,
    reason="open issue: outage_cutoff has a 5 s lock_timeout, so a writer holding the lock "
    "longer makes the note job fail",
)
def test_cutoff_survives_a_slow_writer(seeded_schema: str, app_url: str) -> None:
    writer = open_in_schema(app_url, seeded_schema)
    note = open_in_schema(app_url, seeded_schema)
    note.autocommit = True
    timer = threading.Timer(6.0, writer.commit)
    try:
        take_write_lock(writer)
        timer.start()
        note.execute("SELECT outage_cutoff()").fetchone()
    finally:
        timer.join()
        writer.close()
        note.close()


@pytest.mark.xfail(
    strict=True,
    reason="open issue: take_write_lock in autocommit releases the lock at once, so the "
    "duplicate lookup runs unlocked",
)
def test_write_lock_holds_in_autocommit(seeded_schema: str, app_url: str) -> None:
    conn = open_in_schema(app_url, seeded_schema)
    other = open_in_schema(app_url, seeded_schema)
    conn.autocommit = True
    other.autocommit = True
    try:
        take_write_lock(conn)
        row = other.execute("SELECT pg_try_advisory_lock(%s) AS got", (KEY,)).fetchone()
        assert row is not None
        if row["got"]:
            other.execute("SELECT pg_advisory_unlock(%s)", (KEY,))
        assert not row["got"], "the write lock was free right after take_write_lock"
    finally:
        conn.close()
        other.close()


@pytest.mark.xfail(
    strict=True,
    reason="open issue: corroborate checks only the newest row, so a document linked to an "
    "older row of the chain is linked twice and corroboration_added stays True",
)
def test_corroboration_added_false_when_document_is_on_an_older_row(db: Conn) -> None:
    first = link(db, 40)
    e1 = write_event(db, outage(db, 1, 1), first)
    assert isinstance(e1, Written)
    write_event(db, outage(db, 1, 2, supersedes=e1.id), link(db, 41))
    result = write_event(db, outage(db, 1, 3), first)
    assert isinstance(result, Duplicate), result
    assert not result.corroboration_added, "the same document was linked to the chain twice"
