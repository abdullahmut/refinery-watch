"""The app role may update only the columns that change after insert."""

from datetime import UTC, datetime

import psycopg
import pytest
from tests.db.helpers import add_document, ts

from refinery_watch.db.connect import Conn

pytestmark = pytest.mark.integration

UPDATABLE = {
    "note": {"status", "sent_at", "provider_message_id"},
    "job_run": {"finished_at", "status", "rows_in", "rows_out", "error"},
    "review_item": {"resolved_at", "resolution", "outage_event_id"},
}


@pytest.mark.parametrize("table", sorted(UPDATABLE))
def test_only_the_named_columns_are_updatable(db: Conn, table: str) -> None:
    rows = db.execute(
        "SELECT a.attname AS name,"
        " has_column_privilege('refinery_test_app', a.attrelid, a.attnum, 'UPDATE') AS can"
        " FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0"
        " AND NOT a.attisdropped",
        (table,),
    ).fetchall()
    assert {r["name"] for r in rows if r["can"]} == UPDATABLE[table]


def test_table_level_update_is_not_granted(db: Conn) -> None:
    for table in UPDATABLE:
        row = db.execute(
            "SELECT has_table_privilege('refinery_test_app', %s, 'UPDATE') AS p", (table,)
        ).fetchone()
        assert row == {"p": False}, table


def add_note(conn: Conn) -> None:
    conn.execute(
        "INSERT INTO note (note_date, as_of, status, figures, body_text, body_html, summary,"
        " summary_model, summary_called_at, summary_tokens_in, summary_tokens_out,"
        " summary_cost_usd) VALUES ('2025-11-10', %s, 'built', '{}', 't', 't', 's', 'm', %s,"
        " 1, 1, 0.01)",
        (ts(10), ts(10)),
    )


def test_app_role_cannot_rewrite_the_recorded_cost(app_db: Conn) -> None:
    add_note(app_db)
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as info, app_db.transaction():
        app_db.execute("UPDATE note SET summary_cost_usd = 0")
    assert info.value.sqlstate == "42501"
    with pytest.raises(psycopg.errors.InsufficientPrivilege), app_db.transaction():
        app_db.execute("UPDATE note SET body_text = 'x'")


def test_app_role_can_move_a_note_through_its_states(app_db: Conn) -> None:
    add_note(app_db)
    sent = datetime(2025, 11, 10, 7, tzinfo=UTC)
    app_db.execute(
        "UPDATE note SET status = 'sent', sent_at = %s, provider_message_id = 'm1'", (sent,)
    )
    row = app_db.execute("SELECT status, provider_message_id FROM note").fetchone()
    assert row == {"status": "sent", "provider_message_id": "m1"}


def test_app_role_cannot_rewrite_a_job_run_start_or_a_review_candidate(app_db: Conn) -> None:
    app_db.execute("INSERT INTO job_run (job) VALUES ('fetch')")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), app_db.transaction():
        app_db.execute("UPDATE job_run SET job = 'other'")
    doc = add_document(app_db)
    app_db.execute(
        "INSERT INTO review_item (raw_document_id, candidate, reason) VALUES (%s, '{}', 'x')",
        (doc,),
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege), app_db.transaction():
        app_db.execute("UPDATE review_item SET candidate = '{\"a\": 1}'")
    app_db.execute(
        "UPDATE review_item SET resolved_at = now(), resolution = 'dismissed'",
    )
