"""Regression tests for defects found after the first adapters build."""

import json
from collections.abc import Iterator

import httpx
import psycopg
import pytest
from tests.db.helpers import open_in_schema
from tests.sources.helpers import FakeStore, html_response, make_fetcher, sample_bytes, shut_matcher

from refinery_watch.db.connect import Conn
from refinery_watch.sources import eia_wpsr, gdelt_doc, hp_rss
from refinery_watch.sources.pages import fetch_page
from refinery_watch.sources.store import RunOutcome, finish_run

pytestmark = pytest.mark.integration

EIA_URL = "https://eia.example.com/v2/petroleum/pnp/wiup/data/"


@pytest.fixture
def app(seeded_schema: str, app_url: str) -> Iterator[Conn]:
    conn = open_in_schema(app_url, seeded_schema)
    yield conn
    conn.rollback()
    conn.close()


def new_run(conn: Conn, job: str) -> int:
    row = conn.execute("INSERT INTO job_run (job) VALUES (%s) RETURNING id", (job,)).fetchone()
    assert row is not None
    conn.commit()
    return int(row["id"])


def count(conn: Conn, table: str) -> int:
    row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
    assert row is not None
    return int(row["n"])


def test_eia_document_and_prices_are_one_transaction(
    app: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = sample_bytes("eia_v2_data_handmade.json")

    def broken(*args: object, **kwargs: object) -> int:
        raise psycopg.errors.QueryCanceled("statement timeout")

    monkeypatch.setattr("refinery_watch.sources.eia_wpsr.write_prices", broken)
    first, _, _ = make_fetcher(lambda r: httpx.Response(200, content=body))
    run1 = new_run(app, "fetch_eia_wpsr")
    with pytest.raises(psycopg.errors.QueryCanceled):
        eia_wpsr.run(app, first, FakeStore(), run1, EIA_URL, "K")
    finish_run(app, run1, RunOutcome("failed", 0, 0, "QueryCanceled"))
    assert count(app, "raw_document") == 0
    monkeypatch.undo()
    second, _, _ = make_fetcher(lambda r: httpx.Response(200, content=body))
    outcome = eia_wpsr.run(app, second, FakeStore(), new_run(app, "fetch_eia_wpsr"), EIA_URL, "K")
    assert outcome.rows_out == 3
    assert count(app, "price") == 3
    assert count(app, "raw_document") == 1


POISON = (
    b'<html><head><meta property="article:published_time"'
    b' content="0001-01-01T00:00:00+01:00"></head>'
    b"<body><article><p>A placeholder date.</p></article></body></html>"
)


def lead_list(*paths: str) -> bytes:
    articles = [
        {
            "url": f"https://news.example.com/{p}",
            "title": "t",
            "seendate": "20261110T143000Z",
            "language": "English",
        }
        for p in paths
    ]
    return json.dumps({"articles": articles}).encode()


def test_one_bad_page_does_not_abort_the_gdelt_run(
    app: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    artlist = lead_list("poison", "broken", "a")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/doc/doc":
            return httpx.Response(200, content=artlist)
        if request.url.path == "/poison":
            return httpx.Response(200, content=POISON, headers={"content-type": "text/html"})
        return html_response("article_handmade.html")

    def explode(html: str) -> object:
        raise ValueError("parser bug")

    real = fetch_page

    def picky(fetcher: object, url: str) -> object:
        if url.endswith("/broken"):
            explode("")
        return real(fetcher, url)  # type: ignore[arg-type]

    monkeypatch.setattr("refinery_watch.sources.gdelt_doc.fetch_page", picky)
    fetcher, _, _ = make_fetcher(handler)
    outcome = gdelt_doc.run(
        app,
        fetcher,
        FakeStore(),
        shut_matcher,
        new_run(app, "fetch_gdelt_doc"),
        "https://gdelt.example.com/api/v2/doc/doc",
        60,
        float("inf"),
    )
    assert outcome.status == "ok"
    assert outcome.note is not None
    assert "parse_error=1" in outcome.note
    assert "no_pubdate=1" in outcome.note
    assert count(app, "raw_document") == 1


def test_one_bad_page_does_not_abort_the_hp_run(app: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    feed = (
        b"<rss><channel>"
        b"<item><title>bad</title><link>https://hp.example.com/news/bad</link></item>"
        b"<item><title>Testville shut</title><link>https://hp.example.com/news/a</link></item>"
        b"</channel></rss>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rss":
            return httpx.Response(200, content=feed)
        return html_response("article_handmade.html")

    real = fetch_page

    def picky(fetcher: object, url: str) -> object:
        if url.endswith("/bad"):
            raise OverflowError("date")
        return real(fetcher, url)  # type: ignore[arg-type]

    monkeypatch.setattr("refinery_watch.sources.hp_rss.fetch_page", picky)
    fetcher, _, _ = make_fetcher(handler)
    outcome = hp_rss.run(
        app,
        fetcher,
        FakeStore(),
        shut_matcher,
        "hp_news",
        new_run(app, "fetch_hp_news"),
        "https://hp.example.com/rss",
        float("inf"),
    )
    assert (outcome.status, outcome.rows_out) == ("ok", 1)
    assert outcome.note == "parse_error=1"
