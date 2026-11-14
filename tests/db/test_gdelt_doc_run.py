from collections.abc import Iterator

import httpx
import pytest
from tests.db.helpers import open_in_schema
from tests.sources.helpers import (
    FakeStore,
    Handler,
    html_response,
    make_fetcher,
    sample_bytes,
    shut_matcher,
)

from refinery_watch.db.connect import Conn
from refinery_watch.sources.gdelt_doc import run
from refinery_watch.sources.http import Fetcher
from refinery_watch.sources.store import RunOutcome, finish_run

pytestmark = pytest.mark.integration

FEED_URL = "https://gdelt.example.com/api/v2/doc/doc"
INF = float("inf")
HTML = {"content-type": "text/html"}
STALE = (
    b'<html><head><meta property="article:published_time" content="2026-10-10T00:00:00Z">'
    b"</head><body><article><p>An old story about a refinery that was shut.</p></article>"
    b"</body></html>"
)


def lead_list(*urls: str) -> bytes:
    items = ", ".join(
        f'{{"url": "{u}", "title": "Lead title", "seendate": "20261110T143000Z",'
        f' "language": "English"}}'
        for u in urls
    )
    return f'{{"articles": [{items}]}}'.encode()


def site(artlist: bytes, pages: dict[str, httpx.Response]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/doc/doc":
            return httpx.Response(200, content=artlist)
        return pages.get(f"{request.url.host}{request.url.path}", httpx.Response(404))

    return handler


@pytest.fixture
def app(seeded_schema: str, app_url: str) -> Iterator[Conn]:
    conn = open_in_schema(app_url, seeded_schema)
    yield conn
    conn.rollback()
    conn.close()


def go(conn: Conn, fetcher: Fetcher, store: FakeStore, deadline: float = INF) -> RunOutcome:
    row = conn.execute("INSERT INTO job_run (job) VALUES ('fetch_gdelt_doc') RETURNING id")
    found = row.fetchone()
    assert found is not None
    conn.commit()
    run_id = int(found["id"])
    outcome = run(conn, fetcher, store, shut_matcher, run_id, FEED_URL, 60, deadline)
    finish_run(conn, run_id, outcome)
    return outcome


def handmade_pages() -> dict[str, httpx.Response]:
    return {
        "news.example.com/a": html_response("article_handmade.html"),
        "news.example.org/b": html_response("article_no_date_handmade.html"),
        "news.example.com/c": httpx.Response(200, content=STALE, headers=HTML),
    }


def test_three_leads_one_document(app: Conn) -> None:
    feed = sample_bytes("gdelt_artlist_handmade.json")
    fetcher, net, _ = make_fetcher(site(feed, handmade_pages()))
    store = FakeStore()
    outcome = go(app, fetcher, store)
    assert outcome.status == "ok"
    assert (outcome.rows_in, outcome.rows_out) == (4, 1)
    assert outcome.note is not None
    assert "no_pubdate=1" in outcome.note
    assert "stale=1" in outcome.note
    assert "not_english=1" in outcome.note
    rows = app.execute("SELECT source_id, url, headline FROM raw_document").fetchall()
    assert len(rows) == 1
    assert rows[0]["source_id"] == "gdelt_doc"
    assert rows[0]["url"] == "https://news.example.com/a"
    assert rows[0]["headline"] == "Testville Refinery crude unit shut after fire"
    assert len(store.objects) == 1
    assert "noticias.example.org" not in [r.url.host for r in net.requests]
    assert net.requests[0].url.params["timespan"] == "120min"


def test_publication_time_is_the_page_time_not_seendate(app: Conn) -> None:
    feed = sample_bytes("gdelt_artlist_handmade.json")
    fetcher, _, _ = make_fetcher(site(feed, handmade_pages()))
    go(app, fetcher, FakeStore())
    row = app.execute("SELECT published_at, published_tz FROM raw_document").fetchone()
    assert row is not None
    assert row["published_at"].hour == 12
    assert row["published_tz"] == "Etc/GMT-8"


def test_plain_text_reply_fails_the_run_with_the_text(app: Conn) -> None:
    text = b"Please limit requests to one every 5 seconds"
    fetcher, _, _ = make_fetcher(site(text, {}))
    outcome = go(app, fetcher, FakeStore())
    assert outcome.status == "failed"
    assert outcome.note is not None
    assert "Please limit requests to one every 5 seconds" in outcome.note
    row = app.execute("SELECT status, error FROM job_run ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["status"] == "failed"
    assert "Please limit requests" in row["error"]


def test_empty_result_is_ok(app: Conn) -> None:
    fetcher, _, _ = make_fetcher(site(b"{}", {}))
    outcome = go(app, fetcher, FakeStore())
    assert (outcome.status, outcome.rows_in, outcome.rows_out, outcome.note) == ("ok", 0, 0, None)


def test_blocked_api_host_fails_the_run(app: Conn) -> None:
    fetcher, _, _ = make_fetcher(lambda r: httpx.Response(403))
    outcome = go(app, fetcher, FakeStore())
    assert outcome.status == "failed"
    assert outcome.note is not None
    assert outcome.note.startswith("HostBlocked")


def test_blocked_publisher_host_skips_its_other_leads(app: Conn) -> None:
    pages = handmade_pages()
    pages["news.example.com/a"] = httpx.Response(403)
    feed = sample_bytes("gdelt_artlist_handmade.json")
    fetcher, net, _ = make_fetcher(site(feed, pages))
    outcome = go(app, fetcher, FakeStore())
    assert outcome.status == "ok"
    assert outcome.note is not None
    assert "blocked_host=2" in outcome.note
    assert "blocked hosts: news.example.com" in outcome.note
    assert [r.url.path for r in net.requests if r.url.host == "news.example.com"] == ["/a"]


def test_known_url_is_not_fetched_again(app: Conn) -> None:
    feed = sample_bytes("gdelt_artlist_handmade.json")
    fetcher, net, _ = make_fetcher(site(feed, handmade_pages()))
    store = FakeStore()
    go(app, fetcher, store)
    before = len(net.requests)
    outcome = go(app, fetcher, store)
    assert outcome.rows_out == 0
    assert outcome.note is not None
    assert "known_url=1" in outcome.note
    assert "/a" not in [r.url.path for r in net.requests[before:]]


def test_future_dated_page_is_dropped(app: Conn) -> None:
    future = (
        b'<html><head><meta property="article:published_time" content="2030-01-01T00:00:00Z">'
        b"</head><body><p>shut</p></body></html>"
    )
    pages = {"news.example.com/f": httpx.Response(200, content=future, headers=HTML)}
    fetcher, _, _ = make_fetcher(site(lead_list("https://news.example.com/f"), pages))
    outcome = go(app, fetcher, FakeStore())
    assert outcome.rows_out == 0
    assert outcome.note == "future_pubdate=1"


def test_headline_falls_back_to_the_lead_title(app: Conn) -> None:
    html = (
        b'<html><head><meta property="article:published_time" content="2026-11-10T00:00:00Z">'
        b"</head><body><p>Refinery shut today.</p></body></html>"
    )
    pages = {"news.example.com/g": httpx.Response(200, content=html, headers=HTML)}
    fetcher, _, _ = make_fetcher(site(lead_list("https://news.example.com/g"), pages))
    go(app, fetcher, FakeStore())
    row = app.execute("SELECT headline, prefilter_passed FROM raw_document").fetchone()
    assert row is not None
    assert row["headline"] == "Lead title"
    assert row["prefilter_passed"] is True
