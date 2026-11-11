"""A cut-off from outage_cutoff() sees every row that can ever have known_at at or before it."""

import threading
import time
from datetime import datetime

import pytest
from tests.db.helpers import add_document, insert_outage, open_in_schema, unit_id

from refinery_watch.db.connect import Conn

pytestmark = pytest.mark.integration


def count_open(conn: Conn, as_of: datetime) -> int:
    row = conn.execute("SELECT count(*) AS n FROM outage_as_of(%s)", (as_of,)).fetchone()
    assert row is not None
    return int(row["n"])


def write_committed(conn: Conn, code: str, doc: int) -> datetime:
    event = insert_outage(conn, known_at=None, refinery_unit_id=unit_id(conn, code=code))
    known = conn.execute("SELECT known_at FROM outage_event WHERE id = %s", (event,)).fetchone()
    assert known is not None
    conn.execute(
        "INSERT INTO event_source (outage_event_id, raw_document_id, role, source_kind, evidence)"
        " VALUES (%s, %s, 'primary', 'other_media', 'q')",
        (event, add_document(conn, doc)),
    )
    return known["known_at"]  # type: ignore[no-any-return]


def test_cutoff_waits_for_an_uncommitted_writer_and_then_stays_stable(
    seeded_schema: str, app_url: str
) -> None:
    writer = open_in_schema(app_url, seeded_schema)
    reader = open_in_schema(app_url, seeded_schema)
    reader.autocommit = True
    result: list[datetime] = []
    try:
        known = write_committed(writer, "CDU", 1)
        thread = threading.Thread(
            target=lambda: result.append(
                reader.execute("SELECT outage_cutoff() AS t").fetchone()["t"]  # type: ignore[index]
            )
        )
        thread.start()
        time.sleep(0.6)
        assert thread.is_alive(), "cutoff did not wait for the writer holding the lock"
        writer.commit()
        thread.join(timeout=5)
        assert not thread.is_alive()
        cutoff = result[0]
        assert cutoff >= known
        before = count_open(reader, cutoff)
        write_committed(writer, "VDU", 2)
        writer.commit()
        assert count_open(reader, cutoff) == before == 1
    finally:
        writer.close()
        reader.close()


def test_plain_clock_cutoff_can_miss_a_row_that_commits_later(
    seeded_schema: str, app_url: str
) -> None:
    """Why outage_cutoff exists: a clock read does not wait for the writer."""
    writer = open_in_schema(app_url, seeded_schema)
    reader = open_in_schema(app_url, seeded_schema)
    reader.autocommit = True
    try:
        known = write_committed(writer, "CDU", 1)
        time.sleep(0.2)
        row = reader.execute("SELECT clock_timestamp() AS t").fetchone()
        assert row is not None
        clock = row["t"]
        first = count_open(reader, clock)
        writer.commit()
        assert known < clock
        assert count_open(reader, clock) != first
    finally:
        writer.close()
        reader.close()


def test_cutoff_is_granted_to_the_app_role_not_public(app_db: Conn, db: Conn) -> None:
    row = app_db.execute("SELECT outage_cutoff() AS t").fetchone()
    assert row is not None
    public = db.execute(
        "SELECT has_function_privilege('public', 'outage_cutoff()', 'EXECUTE') AS p"
    ).fetchone()
    assert public == {"p": False}
