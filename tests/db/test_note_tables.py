from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from tests.db.helpers import add_document, ts

from refinery_watch.db.connect import Conn

pytestmark = pytest.mark.integration

GRANTS = {
    "note": {"SELECT", "INSERT", "UPDATE"},
    "price": {"SELECT", "INSERT"},
    "tceq_event": {"SELECT", "INSERT"},
    "llm_spend": {"SELECT"},
}


def add_note(conn: Conn, **over: Any) -> int:
    row: dict[str, Any] = {
        "note_date": "2025-11-10",
        "as_of": ts(10),
        "status": "built",
        "figures": "{}",
        "body_text": "t",
        "body_html": "<p>t</p>",
    }
    row.update(over)
    cols = ", ".join(row)
    marks = ", ".join(f"%({k})s" for k in row)
    found = conn.execute(f"INSERT INTO note ({cols}) VALUES ({marks}) RETURNING id", row)
    result = found.fetchone()
    assert result is not None
    return int(result["id"])


def add_extraction(conn: Conn, n: int, called_at: datetime, cost: str) -> None:
    conn.execute(
        "INSERT INTO extraction (raw_document_id, model, prompt_version, called_at, tokens_in,"
        " tokens_out, cost_usd, status, output) VALUES (%s, 'm', 'extract_v1', %s, 1, 1, %s,"
        " 'ok', '{}')",
        (add_document(conn, n), called_at, Decimal(cost)),
    )


def spend(conn: Conn, start: datetime, end: datetime) -> Decimal:
    found = conn.execute("SELECT llm_spend_usd(%s, %s) AS usd", (start, end)).fetchone()
    assert found is not None
    value = found["usd"]
    assert isinstance(value, Decimal)
    return value


def test_spend_sums_extractions_and_note_summaries(db: Conn) -> None:
    add_extraction(db, 1, ts(10, 1), "0.010000")
    add_extraction(db, 2, ts(10, 2), "0.020000")
    add_note(
        db,
        summary="s",
        summary_model="sonnet",
        summary_called_at=ts(10, 3),
        summary_tokens_in=10,
        summary_tokens_out=5,
        summary_cost_usd=Decimal("0.040000"),
    )
    assert spend(db, ts(10, 0), ts(11, 0)) == Decimal("0.07")


def test_spend_range_is_half_open(db: Conn) -> None:
    add_extraction(db, 1, ts(10, 6), "0.010000")
    assert spend(db, ts(10, 0), ts(10, 6)) == 0
    assert spend(db, ts(10, 6), ts(10, 7)) == Decimal("0.01")


def test_spend_of_an_empty_day_is_zero(db: Conn) -> None:
    assert spend(db, ts(1), ts(2)) == 0


def test_spend_ignores_notes_without_a_summary_call(db: Conn) -> None:
    add_note(db)
    assert spend(db, ts(1), ts(30)) == 0


def test_second_sent_note_for_a_date_is_refused(db: Conn) -> None:
    add_note(db, status="sent", sent_at=ts(10))
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        add_note(db, status="sent", sent_at=ts(10, 7))
    add_note(db, status="send_failed")
    add_note(db, status="sent", sent_at=ts(11), note_date="2025-11-11")


def test_sent_needs_sent_at_and_back(db: Conn) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        add_note(db, status="sent")
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        add_note(db, status="built", sent_at=ts(10))


@pytest.mark.parametrize(
    "bad",
    [
        {"summary_model": "sonnet"},
        {
            "summary_model": "sonnet",
            "summary_called_at": ts(10),
            "summary_tokens_in": 1,
            "summary_tokens_out": 1,
        },
        {"summary": "text"},
        {"status": "weird"},
        {
            "summary_model": "sonnet",
            "summary_called_at": ts(10),
            "summary_tokens_in": 1,
            "summary_tokens_out": 1,
            "summary_cost_usd": -1,
        },
    ],
)
def test_note_summary_checks(db: Conn, bad: dict[str, Any]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        add_note(db, **bad)


def test_note_can_link_to_the_previous_note(db: Conn) -> None:
    first = add_note(db)
    add_note(db, previous_note_id=first, note_date="2025-11-11")


def test_price_revision_is_a_new_row(db: Conn) -> None:
    for known, value in ((ts(10), "80.0"), (ts(11), "81.5")):
        db.execute(
            "INSERT INTO price (series, observed_on, value, unit, source_id, known_at)"
            " VALUES ('eia.util.us', '2025-11-07', %s, 'pct', 'fixture_src', %s)",
            (Decimal(value), known),
        )
    latest = db.execute(
        "SELECT value FROM price WHERE series = 'eia.util.us' AND known_at <= %s"
        " ORDER BY known_at DESC LIMIT 1",
        (ts(10, 12),),
    ).fetchone()
    assert latest == {"value": Decimal("80.0000")}
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        db.execute(
            "INSERT INTO price (series, observed_on, value, unit, source_id, known_at)"
            " VALUES ('eia.util.us', '2025-11-07', 1, 'pct', 'fixture_src', %s)",
            (ts(11),),
        )
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute(
            "INSERT INTO price (series, observed_on, value, unit, source_id)"
            " VALUES ('Bad Series', '2025-11-07', 1, 'pct', 'fixture_src')"
        )


def test_tceq_event_is_unique_per_incident_and_stage(db: Conn) -> None:
    doc = add_document(db)
    sql = (
        "INSERT INTO tceq_event (incident_id, report_stage, raw_document_id, regulated_entity,"
        " event_kind, report_url) VALUES ('1', %s, %s, 'Plant', 'emission', 'https://x.test')"
    )
    db.execute(sql, ("initial", doc))
    db.execute(sql, ("final", doc))
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        db.execute(sql, ("final", doc))
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute(sql, ("draft", doc))


def test_app_grants_match_table(privileges: Callable[[str], set[str]]) -> None:
    for table, expected in GRANTS.items():
        assert privileges(table) == expected, table


def test_app_role_can_call_the_spend_function(db: Conn, app_db: Conn) -> None:
    assert spend(app_db, ts(1), ts(2)) == 0
    row = db.execute(
        "SELECT has_function_privilege('public', 'llm_spend_usd(timestamptz, timestamptz)',"
        " 'EXECUTE') AS p"
    ).fetchone()
    assert row == {"p": False}


def test_head_revision_is_0008(db: Conn, session_schema: str) -> None:
    row = db.execute(f"SELECT version_num FROM {session_schema}.alembic_version").fetchone()
    assert row == {"version_num": "0008"}
