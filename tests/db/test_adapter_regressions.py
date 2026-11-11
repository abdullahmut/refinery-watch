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
from refinery_watch.sources.store import (
    RunNotRunning,
    RunOutcome,
    finish_run,
    start_run,
    url_known,
)

pytestmark = pytest.mark.integration

EIA_URL = "https://eia.example.com/v2/petroleum/pnp/wiup/data/"


@pytest.fixture
def owner(seeded_schema: str, pg_server: str) -> Iterator[Conn]:
    conn = open_in_schema(pg_server, seeded_schema)
    yield conn
    conn.rollback()
    conn.close()


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
        eia_wpsr.run(app, first, FakeStore(), run1, EIA_URL, "K", float("inf"))
    finish_run(app, run1, RunOutcome("failed", 0, 0, "QueryCanceled"))
    assert count(app, "raw_document") == 0
    monkeypatch.undo()
    second, _, _ = make_fetcher(lambda r: httpx.Response(200, content=body))
    outcome = eia_wpsr.run(
        app, second, FakeStore(), new_run(app, "fetch_eia_wpsr"), EIA_URL, "K", float("inf")
    )
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
    app: Conn, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
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
    assert "page failed to parse" in caplog.text
    assert "no_pubdate=1" in outcome.note
    assert count(app, "raw_document") == 1


def test_one_bad_page_does_not_abort_the_hp_run(
    app: Conn, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
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
    assert "page failed to parse" in caplog.text


def test_finish_of_a_swept_run_is_not_silently_dropped(app: Conn, owner: Conn) -> None:
    job = "fetch_hp_news"
    first = start_run(app, job, 1)
    assert first is not None
    owner.execute(
        "UPDATE job_run SET started_at = clock_timestamp() - interval '41 minutes' WHERE id = %s",
        (first,),
    )
    owner.commit()
    second = start_run(app, job, 1)
    assert second is not None
    with pytest.raises(RunNotRunning):
        finish_run(app, first, RunOutcome("ok", 5, 5, None))
    row = app.execute("SELECT status FROM job_run WHERE id = %s", (first,)).fetchone()
    assert row is not None
    assert row["status"] == "failed"


def test_cli_exits_1_when_the_run_row_was_lost(
    seeded_schema: str, app_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.parse import quote

    from refinery_watch.sources import __main__ as cli

    env = {
        "DATABASE_URL": f"{app_url}?options={quote(f'-c search_path={seeded_schema}')}",
        "CONTACT_EMAIL": "contact@example.com",
        "OBJECT_STORE_ENDPOINT": "https://store.example.com",
        "OBJECT_STORE_REGION": "test-1",
        "OBJECT_STORE_BUCKET_RAW": "rw-raw-test",
        "OBJECT_STORE_ACCESS_KEY": "access-id",
        "OBJECT_STORE_SECRET_KEY": "secret-value-xyz",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    def lost(conn: object, run_id: int, outcome: object) -> None:
        raise RunNotRunning("lost")

    monkeypatch.setattr(cli, "finish_run", lost)
    t = httpx.MockTransport(lambda r: httpx.Response(404))
    code = cli.main(
        ["hp_news"], transport=t, resolve=lambda h: ["93.184.216.34"], store=FakeStore()
    )
    assert code == 1


def test_finish_run_note_has_no_control_characters(app: Conn) -> None:
    run_id = start_run(app, "fetch_hp_news", 1)
    assert run_id is not None
    finish_run(app, run_id, RunOutcome("failed", 0, 0, "first\nsecond\x1b[0m\r\nthird"))
    row = app.execute("SELECT error FROM job_run WHERE id = %s", (run_id,)).fetchone()
    assert row is not None
    assert row["error"] == "first second [0m third"


def run_hp_then_gdelt(app: Conn, gdelt_url: str) -> tuple[RunOutcome, RunOutcome]:
    page = html_response("article_handmade.html")
    feed = (
        b"<rss><channel><item><title>Feed headline differs from the page</title>"
        b"<link>https://news.example.com/a</link></item></channel></rss>"
    )
    artlist = json.dumps(
        {
            "articles": [
                {
                    "url": gdelt_url,
                    "title": "Lead title differs too",
                    "seendate": "20261110T143000Z",
                    "language": "English",
                }
            ]
        }
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rss":
            return httpx.Response(200, content=feed)
        if request.url.path == "/api/v2/doc/doc":
            return httpx.Response(200, content=artlist)
        return page

    hp_fetcher, _, _ = make_fetcher(handler)
    first = hp_rss.run(
        app,
        hp_fetcher,
        FakeStore(),
        shut_matcher,
        "hp_news",
        new_run(app, "fetch_hp_news"),
        "https://news.example.com/rss",
        float("inf"),
    )
    g_fetcher, _, _ = make_fetcher(handler)
    second = gdelt_doc.run(
        app,
        g_fetcher,
        FakeStore(),
        shut_matcher,
        new_run(app, "fetch_gdelt_doc"),
        "https://gdelt.example.com/api/v2/doc/doc",
        60,
        float("inf"),
    )
    return first, second


def test_same_page_at_the_other_scheme_is_skipped_by_url(app: Conn) -> None:
    _, second = run_hp_then_gdelt(app, "http://news.example.com/a")
    assert count(app, "raw_document") == 1
    assert second.note is not None
    assert "known_url=1" in second.note


def test_same_page_at_another_url_is_skipped_by_hash(app: Conn) -> None:
    _, second = run_hp_then_gdelt(app, "https://news.example.com/a-copy")
    assert count(app, "raw_document") == 1
    assert second.note is not None
    assert "known_hash=1" in second.note


def test_url_known_ignores_the_scheme(app: Conn) -> None:
    run_hp_then_gdelt(app, "https://news.example.com/other")
    assert url_known(app, ["http://news.example.com/a"])
    assert url_known(app, ["https://news.example.com/a"])
    assert not url_known(app, ["http://news.example.com/zzz"])


def test_a_block_on_the_api_host_during_a_lead_fails_the_run(app: Conn) -> None:
    feed = json.dumps(
        {
            "articles": [
                {
                    "url": "https://gdelt.example.com/story",
                    "title": "t",
                    "seendate": "20261110T143000Z",
                    "language": "English",
                }
            ]
        }
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/doc/doc":
            return httpx.Response(200, content=feed)
        return httpx.Response(403)

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
    assert outcome.status == "failed"


def test_a_lead_that_redirects_into_a_blocked_api_host_fails_the_run(app: Conn) -> None:
    feed = lead_list("moved")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/doc/doc" and request.url.host == "gdelt.example.com":
            if request.headers.get("x-blocked"):
                return httpx.Response(403)
            return httpx.Response(200, content=feed)
        if request.url.host == "news.example.com":
            return httpx.Response(302, headers={"location": "https://gdelt.example.com/blocked"})
        return httpx.Response(403)

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
    assert outcome.status == "failed"
    assert outcome.note is not None
    assert outcome.note.startswith("HostBlocked")
