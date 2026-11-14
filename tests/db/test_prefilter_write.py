from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from tests.db.helpers import open_in_schema
from tests.sources.helpers import FakeStore

from refinery_watch.db.connect import Conn
from refinery_watch.extract.prefilter import build_term_matcher
from refinery_watch.extract.reference import load_refineries
from refinery_watch.sources.store import NewDocument, Stored, store_document

pytestmark = pytest.mark.integration


@pytest.fixture
def app(seeded_schema: str, app_url: str) -> Iterator[Conn]:
    conn = open_in_schema(app_url, seeded_schema)
    yield conn
    conn.rollback()
    conn.close()


def doc(text: str, sha_char: str, terms: tuple[str, ...]) -> NewDocument:
    return NewDocument(
        source_id="hp_news",
        url=f"https://hp.example.com/news/{sha_char}",
        original=text.encode(),
        ext="html",
        content_type="text/html",
        content_sha256=sha_char * 64,
        headline=None,
        body_chars=len(text),
        fetched_at=datetime(2026, 11, 10, 12, tzinfo=UTC),
        published=None,
        prefilter_terms=terms,
    )


def test_terms_from_the_matcher_agree_with_prefilter_passed(app: Conn) -> None:
    matcher = build_term_matcher(load_refineries(app))
    run = app.execute("INSERT INTO job_run (job) VALUES ('fetch_hp_news') RETURNING id").fetchone()
    assert run is not None
    app.commit()
    texts = {
        "a": "Testville refinery crude unit was shut after a fire.",
        "b": "The market was shut on Monday.",
        "c": "Testville is a pleasant town.",
    }
    for char, text in texts.items():
        result = store_document(app, FakeStore(), run["id"], doc(text, char, tuple(matcher(text))))
        assert isinstance(result, Stored)
    rows = {
        r["content_sha256"][0]: r
        for r in app.execute(
            "SELECT content_sha256, prefilter_passed, prefilter_terms FROM raw_document"
        )
    }
    assert rows["a"]["prefilter_passed"] is True
    assert rows["a"]["prefilter_terms"] == ["shut", "fire", "refinery:R9001"]
    assert rows["b"]["prefilter_passed"] is False
    assert rows["b"]["prefilter_terms"] == []
    assert rows["c"]["prefilter_passed"] is False
