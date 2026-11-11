from datetime import UTC, datetime, timedelta, timezone

import pytest

from refinery_watch.sources.store import NewDocument, object_key, store_document

SHA = "a" * 64


def test_object_key_layout() -> None:
    fetched = datetime(2026, 11, 10, 22, 0, tzinfo=UTC)
    assert object_key("hp_news", fetched, SHA, "html") == f"raw/hp_news/2026/11/10/{SHA}.html"


def test_object_key_date_is_utc() -> None:
    fetched = datetime(2026, 11, 10, 23, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert object_key("eia_wpsr", fetched, SHA, "json") == f"raw/eia_wpsr/2026/11/11/{SHA}.json"


def test_object_key_refuses_naive_time() -> None:
    with pytest.raises(ValueError, match="aware"):
        object_key("hp_news", datetime(2026, 11, 10), SHA, "html")  # noqa: DTZ001


def test_store_document_refuses_naive_time_before_touching_anything() -> None:
    doc = NewDocument(
        source_id="hp_news",
        url="https://hp.example.com/x",
        original=b"x",
        ext="html",
        content_type="text/html",
        content_sha256=SHA,
        headline=None,
        body_chars=1,
        fetched_at=datetime(2026, 11, 10),  # noqa: DTZ001
        published=None,
        prefilter_terms=(),
    )
    with pytest.raises(ValueError, match="aware"):
        store_document(None, None, 1, doc)  # type: ignore[arg-type]
