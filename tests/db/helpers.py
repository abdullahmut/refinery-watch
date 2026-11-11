"""Shared builders for database tests. Dates move back one year from the repro days."""

from datetime import UTC, date, datetime
from typing import Any

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


def insert_outage(conn: Conn, **over: Any) -> int:
    """Inserts one outage_event row as a root row of its own chain. Returns its id."""
    row = outage_columns(conn, **over)
    new_id = conn.execute(
        "SELECT coalesce(max(id), 8999999) + 1 AS id FROM outage_event"
    ).fetchone()
    assert new_id is not None
    row["id"] = row["chain_id"] = new_id["id"]
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
