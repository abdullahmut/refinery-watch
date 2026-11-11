from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from refinery_watch.db.connect import Conn

pytestmark = pytest.mark.integration

SHA_A = "a" * 64
SHA_B = "b" * 64
GRANTS = {
    "job_run": {"SELECT", "INSERT", "UPDATE"},
    "raw_document": {"SELECT", "INSERT"},
    "extraction": {"SELECT", "INSERT"},
    "review_item": {"SELECT", "INSERT", "UPDATE"},
}


def add_document(db: Conn, sha: str = SHA_A, **over: Any) -> int:
    row: dict[str, Any] = {
        "source": "fixture_src",
        "url": "https://example.test/a",
        "sha": sha,
        "key": f"raw/fixture_src/{sha}.html",
        "chars": 100,
        "published_at": None,
        "published_tz": None,
        "terms": ["refinery"],
        "passed": True,
    }
    row.update(over)
    found = db.execute(
        "INSERT INTO raw_document (source_id, url, content_sha256, object_key, body_chars,"
        " published_at, published_tz, prefilter_passed, prefilter_terms)"
        " VALUES (%(source)s, %(url)s, %(sha)s, %(key)s, %(chars)s, %(published_at)s,"
        " %(published_tz)s, %(passed)s, %(terms)s) RETURNING id",
        row,
    ).fetchone()
    assert found is not None
    return int(found["id"])


def add_extraction(db: Conn, doc: int, status: str = "ok", version: str = "extract_v1") -> int:
    output = '{"events": []}' if status == "ok" else None
    error = None if status == "ok" else "boom"
    found = db.execute(
        "INSERT INTO extraction (raw_document_id, model, prompt_version, tokens_in, tokens_out,"
        " cost_usd, status, output, error)"
        " VALUES (%s, 'm', %s, 10, 5, 0.001, %s, %s, %s) RETURNING id",
        (doc, version, status, output, error),
    ).fetchone()
    assert found is not None
    return int(found["id"])


def test_same_content_hash_is_refused(db: Conn) -> None:
    add_document(db)
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        add_document(db, key="raw/other")
    add_document(db, SHA_B)


@pytest.mark.parametrize(
    "bad",
    [{"sha": "ABC"}, {"sha": "a" * 63}, {"chars": -1}, {"terms": [], "passed": True}],
)
def test_document_checks(db: Conn, bad: dict[str, Any]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        add_document(db, **bad)


def test_prefilter_failed_has_no_terms(db: Conn) -> None:
    add_document(db, terms=[], passed=False)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        add_document(db, SHA_B, terms=["x"], passed=False)


def publication_date(db: Conn, doc: int) -> str:
    row = db.execute("SELECT publication_date AS d FROM raw_document WHERE id = %s", (doc,))
    found = row.fetchone()
    assert found is not None
    return str(found["d"])


def test_publication_date_without_tz_is_utc(db: Conn) -> None:
    pub = datetime(2026, 11, 12, 23, tzinfo=UTC)
    assert publication_date(db, add_document(db, published_at=pub)) == "2026-11-12"


def test_publication_date_uses_published_tz(db: Conn) -> None:
    pub = datetime(2026, 11, 12, 23, tzinfo=UTC)
    doc = add_document(db, published_at=pub, published_tz="Asia/Singapore")
    assert publication_date(db, doc) == "2026-11-13"


def test_publication_date_falls_back_to_fetch_time(db: Conn) -> None:
    doc = add_document(db)
    row = db.execute(
        "SELECT publication_date = (fetched_at AT TIME ZONE 'UTC')::date AS same"
        " FROM raw_document WHERE id = %s",
        (doc,),
    ).fetchone()
    assert row == {"same": True}


def test_invalid_timezone_is_refused(db: Conn) -> None:
    pub = datetime(2026, 11, 12, 23, tzinfo=UTC)
    with pytest.raises(psycopg.Error), db.transaction():
        add_document(db, published_at=pub, published_tz="Not/AZone")


def test_timezone_needs_published_at(db: Conn) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        add_document(db, published_tz="Asia/Singapore")


def test_second_ok_extraction_is_refused(db: Conn) -> None:
    doc = add_document(db)
    add_extraction(db, doc)
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        add_extraction(db, doc)


def test_failed_calls_and_new_prompt_versions_are_accepted(db: Conn) -> None:
    doc = add_document(db)
    add_extraction(db, doc)
    add_extraction(db, doc, status="api_error")
    add_extraction(db, doc, status="invalid_output")
    add_extraction(db, doc, version="extract_v2")
    row = db.execute("SELECT count(*) AS n FROM extraction").fetchone()
    assert row == {"n": 4}


@pytest.mark.parametrize(
    "sql",
    [
        "(raw_document_id, model, prompt_version, tokens_in, tokens_out, cost_usd, status)"
        " VALUES ({d}, 'm', 'extract_v1', 1, 1, 0, 'ok')",
        "(raw_document_id, model, prompt_version, tokens_in, tokens_out, cost_usd, status)"
        " VALUES ({d}, 'm', 'extract_v1', 1, 1, 0, 'api_error')",
        "(raw_document_id, model, prompt_version, tokens_in, tokens_out, cost_usd, status, error)"
        " VALUES ({d}, 'm', 'v1', 1, 1, 0, 'api_error', 'e')",
        "(raw_document_id, model, prompt_version, tokens_in, tokens_out, cost_usd, status, error)"
        " VALUES ({d}, 'm', 'extract_v1', 1, 1, -1, 'api_error', 'e')",
    ],
)
def test_extraction_checks(db: Conn, sql: str) -> None:
    doc = add_document(db)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute(f"INSERT INTO extraction {sql.format(d=doc)}")


def test_two_running_rows_for_one_job_are_refused(db: Conn) -> None:
    db.execute("INSERT INTO job_run (job) VALUES ('fetch')")
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        db.execute("INSERT INTO job_run (job) VALUES ('fetch')")
    db.execute("INSERT INTO job_run (job) VALUES ('other')")


def test_job_run_finished_state(db: Conn) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute("INSERT INTO job_run (job, status) VALUES ('fetch', 'ok')")
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute("INSERT INTO job_run (job, finished_at) VALUES ('fetch', now())")
    db.execute("INSERT INTO job_run (job, status, finished_at) VALUES ('fetch', 'ok', now())")
    db.execute("INSERT INTO job_run (job) VALUES ('fetch')")


def test_review_item_resolution_rules(db: Conn) -> None:
    doc = add_document(db)
    db.execute(
        "INSERT INTO review_item (raw_document_id, candidate, reason) VALUES (%s, '{}', 'x')",
        (doc,),
    )
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute(
            "INSERT INTO review_item (raw_document_id, candidate, reason, resolution)"
            " VALUES (%s, '{}', 'x', 'dismissed')",
            (doc,),
        )
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute(
            "INSERT INTO review_item (raw_document_id, candidate, reason, resolved_at,"
            " resolution) VALUES (%s, '{}', 'x', now(), 'written')",
            (doc,),
        )
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute(
            "INSERT INTO review_item (raw_document_id, candidate, reason) VALUES (%s, '{}', 'X Y')",
            (doc,),
        )


def test_app_grants_match_table(privileges: Callable[[str], set[str]]) -> None:
    for table, expected in GRANTS.items():
        assert privileges(table) == expected, table


def test_app_role_can_write_ingest_rows(app_db: Conn) -> None:
    doc = add_document(app_db, "c" * 64)
    add_extraction(app_db, doc)
    app_db.execute("INSERT INTO job_run (job) VALUES ('fetch')")
    app_db.execute("UPDATE job_run SET status = 'ok', finished_at = now() WHERE job = 'fetch'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_db.execute("DELETE FROM job_run")
