import hashlib
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime

import psycopg
import pytest
from tests.db.helpers import open_in_schema
from tests.sources.helpers import FakeStore

from refinery_watch.db.connect import Conn
from refinery_watch.objects import ObjectStoreError
from refinery_watch.sources.published import Published
from refinery_watch.sources.store import (
    NewDocument,
    Skipped,
    Stored,
    flag_thin_page,
    store_document,
    url_known,
)

pytestmark = pytest.mark.integration

FETCHED = datetime(2026, 11, 10, 22, 0, tzinfo=UTC)


def sha_of(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def make_doc(text: str = "one") -> NewDocument:
    return NewDocument(
        source_id="hp_news",
        url=f"https://hp.example.com/news/{text}",
        original=b"<html>" + text.encode() + b"</html>",
        ext="html",
        content_type="text/html",
        content_sha256=sha_of(text),
        headline="Headline " + text,
        body_chars=777,
        fetched_at=FETCHED,
        published=None,
        prefilter_terms=(),
    )


@pytest.fixture
def app(seeded_schema: str, app_url: str) -> Iterator[Conn]:
    conn = open_in_schema(app_url, seeded_schema)
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture
def run_id(app: Conn) -> int:
    row = app.execute("INSERT INTO job_run (job) VALUES ('fetch_hp_news') RETURNING id").fetchone()
    assert row is not None
    app.commit()
    return int(row["id"])


def one_row(conn: Conn, sha: str) -> dict[str, object]:
    row = conn.execute("SELECT * FROM raw_document WHERE content_sha256 = %s", (sha,)).fetchone()
    assert row is not None
    return dict(row)


def count_rows(conn: Conn) -> int:
    row = conn.execute("SELECT count(*) AS n FROM raw_document").fetchone()
    assert row is not None
    return int(row["n"])


def test_store_writes_one_row_and_one_object(app: Conn, run_id: int) -> None:
    store = FakeStore()
    published = Published(datetime(2026, 11, 10, 20, 0, tzinfo=UTC), "Etc/GMT-8", True)
    doc = replace(make_doc("one"), published=published, prefilter_terms=("shut", "refinery:R9001"))
    result = store_document(app, store, run_id, doc)
    sha = sha_of("one")
    key = f"raw/hp_news/2026/11/10/{sha}.html"
    assert isinstance(result, Stored)
    assert result.object_key == key
    assert store.objects == {key: doc.original}
    assert store.types[key] == "text/html"
    row = one_row(app, sha)
    assert row["object_key"] == key
    assert row["body_chars"] == 777
    assert row["prefilter_passed"] is True
    assert row["prefilter_terms"] == ["shut", "refinery:R9001"]
    assert str(row["publication_date"]) == "2026-11-11"
    assert row["published_tz"] == "Etc/GMT-8"
    assert row["job_run_id"] == run_id
    assert row["id"] == result.raw_document_id


def test_prefilter_passed_follows_terms(app: Conn, run_id: int) -> None:
    store_document(app, FakeStore(), run_id, make_doc("none"))
    row = one_row(app, sha_of("none"))
    assert row["prefilter_passed"] is False
    assert row["published_at"] is None


def test_same_hash_twice_is_skipped_without_a_put(app: Conn, run_id: int) -> None:
    store = FakeStore()
    store_document(app, store, run_id, make_doc("one"))
    again = store_document(
        app, store, run_id, replace(make_doc("one"), url="https://hp.example.com/other")
    )
    assert again == Skipped("known_hash", sha_of("one"))
    assert store.puts == 1


def test_row_stored_by_another_job_between_check_and_insert(
    app: Conn, run_id: int, seeded_schema: str, app_url: str
) -> None:
    other = open_in_schema(app_url, seeded_schema)
    store = FakeStore()

    def race(key: str) -> None:
        store_document(other, FakeStore(), run_id, make_doc("race"))

    store.on_put = race
    result = store_document(app, store, run_id, make_doc("race"))
    other.close()
    assert result == Skipped("known_hash", sha_of("race"))
    assert count_rows(app) == 1


def test_failing_put_writes_no_row(app: Conn, run_id: int) -> None:
    store = FakeStore()
    store.fail = True
    with pytest.raises(ObjectStoreError):
        store_document(app, store, run_id, make_doc("one"))
    app.rollback()
    assert count_rows(app) == 0


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("UTC", "2026-11-10"),
        ("Etc/GMT-14", "2026-11-11"),
        ("Etc/GMT+12", "2026-11-10"),
        ("Asia/Kolkata", "2026-11-11"),
        ("Asia/Kathmandu", "2026-11-11"),
        ("Asia/Yangon", "2026-11-11"),
        ("Asia/Tehran", "2026-11-11"),
        ("Australia/Darwin", "2026-11-11"),
        (None, "2026-11-10"),
    ],
)
def test_every_zone_name_is_accepted_and_dates_correctly(
    app: Conn, run_id: int, name: str | None, expected: str
) -> None:
    published = Published(datetime(2026, 11, 10, 23, 30, tzinfo=UTC), name, name is not None)
    text = f"zone-{name}"
    store_document(app, FakeStore(), run_id, replace(make_doc(text), published=published))
    assert str(one_row(app, sha_of(text))["publication_date"]) == expected


def test_url_known(app: Conn, run_id: int) -> None:
    doc = make_doc("one")
    assert not url_known(app, [doc.url])
    store_document(app, FakeStore(), run_id, doc)
    assert url_known(app, ["https://nope.example.com/", doc.url])
    assert not url_known(app, ["https://nope.example.com/"])
    assert not url_known(app, [])


def test_flag_thin_page(app: Conn, run_id: int) -> None:
    stored = store_document(app, FakeStore(), run_id, make_doc("thin"))
    assert isinstance(stored, Stored)
    item_id = flag_thin_page(app, stored.raw_document_id, "https://hp.example.com/t", 120, 2)
    row = app.execute("SELECT * FROM review_item WHERE id = %s", (item_id,)).fetchone()
    assert row is not None
    assert row["reason"] == "thin_page"
    assert row["resolved_at"] is None
    assert row["raw_document_id"] == stored.raw_document_id
    assert row["candidate"] == {"url": "https://hp.example.com/t", "body_chars": 120, "images": 2}


def test_unrelated_integrity_error_is_not_swallowed(app: Conn, run_id: int) -> None:
    bad = replace(make_doc("badsource"), source_id="no_such_source")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        store_document(app, FakeStore(), run_id, bad)
