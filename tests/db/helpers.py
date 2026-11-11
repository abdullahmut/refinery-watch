"""Shared builders for database tests. Dates move back one year from the repro days."""

from datetime import UTC, date, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from refinery_watch.db.connect import Conn


def ts(day: int, hour: int = 6) -> datetime:
    return datetime(2025, 11, day, hour, tzinfo=UTC)


def day(d: int) -> date:
    return date(2025, 11, d)


def unit_id(conn: Conn, refinery_id: str = "R9001", code: str = "CDU") -> int:
    row = conn.execute(
        "SELECT id FROM refinery_unit WHERE refinery_id = %s AND unit_code = %s AND unit_tag = ''",
        (refinery_id, code),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def add_document(conn: Conn, n: int = 1) -> int:
    sha = f"{n:064x}"
    row = conn.execute(
        "INSERT INTO raw_document (source_id, url, content_sha256, object_key, body_chars,"
        " prefilter_passed, prefilter_terms)"
        " VALUES ('fixture_src', %s, %s, %s, 100, true, '{refinery}') RETURNING id",
        (f"https://example.test/{n}", sha, f"raw/fixture_src/{sha}.html"),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def outage_columns(conn: Conn, **over: Any) -> dict[str, Any]:
    """Column values for a root unplanned outage on R9001's CDU."""
    row: dict[str, Any] = {
        "event_type": "unplanned",
        "refinery_id": "R9001",
        "refinery_unit_id": unit_id(conn),
        "whole_plant": False,
        "capacity_affected_kbd": 200,
        "capacity_basis": "stated",
        "start_date": day(1),
        "confidence": "high",
        "match_score": 100,
        "known_at": ts(1),
    }
    row.update(over)
    return row


def insert_raw_outage(conn: Conn, **over: Any) -> int:
    """Inserts a row with explicit id and chain_id, past the insert trigger. Owner only."""
    state = conn.execute(
        "SELECT tgenabled FROM pg_trigger"
        " WHERE tgrelid = 'outage_event'::regclass AND tgname = 'outage_event_before_insert'"
    ).fetchone()
    if state is not None and state["tgenabled"] != "D":
        conn.execute("ALTER TABLE outage_event DISABLE TRIGGER outage_event_before_insert")
    row = outage_columns(conn, **over)
    new_id = conn.execute(
        "SELECT coalesce(max(id), 8999999) + 1 AS id FROM outage_event"
    ).fetchone()
    assert new_id is not None
    row["id"] = new_id["id"]
    row.setdefault("chain_id", row["id"])
    cols = ", ".join(row)
    marks = ", ".join(f"%({k})s" for k in row)
    conn.execute(f"INSERT INTO outage_event ({cols}) OVERRIDING SYSTEM VALUE VALUES ({marks})", row)
    return int(row["id"])


def add_source(
    conn: Conn,
    outage_id: int,
    document_id: int,
    role: str = "primary",
    known_at: datetime | None = None,
) -> int:
    row = conn.execute(
        "INSERT INTO event_source (outage_event_id, raw_document_id, role, source_kind,"
        " evidence, known_at) VALUES (%s, %s, %s, 'company_statement', 'quoted text', %s)"
        " RETURNING id",
        (outage_id, document_id, role, known_at),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def insert_raw_restart(conn: Conn, ends_event_id: int, **over: Any) -> int:
    fields: dict[str, Any] = {
        "event_type": "restart",
        "ends_event_id": ends_event_id,
        "capacity_basis": "not_stated",
        "capacity_affected_kbd": None,
    }
    fields.update(over)
    return insert_raw_outage(conn, **fields)


def current_ids(conn: Conn, as_of: datetime) -> list[int]:
    rows = conn.execute("SELECT id FROM outage_as_of(%s) ORDER BY id", (as_of,)).fetchall()
    return [int(r["id"]) for r in rows]


def sourced(conn: Conn, event_id: int) -> int:
    """Gives a row its primary source. Returns the row id."""
    add_source(conn, event_id, add_document(conn, event_id))
    return event_id


def insert_outage(conn: Conn, **over: Any) -> int:
    """Inserts a row through the insert trigger. Returns its id."""
    row = outage_columns(conn, **over)
    cols = ", ".join(row)
    marks = ", ".join(f"%({k})s" for k in row)
    found = conn.execute(
        f"INSERT INTO outage_event ({cols}) VALUES ({marks}) RETURNING id", row
    ).fetchone()
    assert found is not None
    return int(found["id"])


def insert_restart(conn: Conn, **over: Any) -> int:
    fields: dict[str, Any] = {
        "event_type": "restart",
        "capacity_basis": "not_stated",
        "capacity_affected_kbd": None,
    }
    fields.update(over)
    return insert_outage(conn, **fields)


def write(conn: Conn, **over: Any) -> int:
    """An outage row through the trigger, with its primary source."""
    return sourced(conn, insert_outage(conn, **over))


def write_restart(conn: Conn, **over: Any) -> int:
    return sourced(conn, insert_restart(conn, **over))


def assert_unit_exclusive(conn: Conn, times: list[datetime]) -> None:
    """At every time, outage_as_of holds at most one row per unit key."""
    for t in times:
        rows = conn.execute(
            "SELECT refinery_id, refinery_unit_id, count(*) AS n FROM outage_as_of(%s)"
            " GROUP BY refinery_id, refinery_unit_id HAVING count(*) > 1",
            (t,),
        ).fetchall()
        assert rows == [], f"two open rows for one unit at {t}"


def open_in_schema(url: str, schema: str) -> Conn:
    return psycopg.connect(
        url,
        row_factory=dict_row,
        autocommit=False,
        application_name="refinery-watch",
        options=f"-c search_path={schema}",
    )
