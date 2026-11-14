from collections.abc import Iterator

import httpx
import pytest
from tests.db.helpers import open_in_schema
from tests.sources.helpers import (
    Clock,
    FakeStore,
    Handler,
    html_response,
    make_fetcher,
    never,
    sample_bytes,
    shut_matcher,
)

from refinery_watch.db.connect import Conn
from refinery_watch.sources.hp_rss import run
from refinery_watch.sources.http import Fetcher
from refinery_watch.sources.store import RunOutcome, finish_run
from refinery_watch.terms import TermMatcher

pytestmark = pytest.mark.integration

FEED_URL = "https://hp.example.com/rss?feed=news"
INF = float("inf")


def rss(*links: str) -> bytes:
    items = "".join(
        f"<item><title>Item {n} shut</title><link>{link}</link></item>"
        for n, link in enumerate(links)
    )
    return f"<rss version='2.0'><channel>{items}</channel></rss>".encode()


def site(feed: bytes, pages: dict[str, httpx.Response]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rss":
            return httpx.Response(200, content=feed, headers={"content-type": "application/xml"})
        return pages.get(request.url.path, httpx.Response(404))

    return handler


@pytest.fixture
def app(seeded_schema: str, app_url: str) -> Iterator[Conn]:
    conn = open_in_schema(app_url, seeded_schema)
    yield conn
    conn.rollback()
    conn.close()


def new_run(conn: Conn) -> int:
    row = conn.execute("INSERT INTO job_run (job) VALUES ('fetch_hp_news') RETURNING id").fetchone()
    assert row is not None
    conn.commit()
    return int(row["id"])


def count(conn: Conn, table: str) -> int:
    row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
    assert row is not None
    return int(row["n"])


def go(
    conn: Conn,
    fetcher: Fetcher,
    store: FakeStore,
    matcher: TermMatcher = shut_matcher,
    deadline: float = INF,
) -> RunOutcome:
    run_id = new_run(conn)
    outcome = run(conn, fetcher, store, matcher, "hp_news", run_id, FEED_URL, deadline)
    finish_run(conn, run_id, outcome)
    return outcome


def test_handmade_feed_stores_three_documents(app: Conn) -> None:
    pages = {
        "/news/testville-fire": html_response("article_handmade.html"),
        "/news/samplebay-restart": html_response("article_no_date_handmade.html"),
        "/news/turnaround-roundup": html_response("article_thin_handmade.html"),
    }
    fetcher, net, _ = make_fetcher(site(sample_bytes("rss_feed_handmade.xml"), pages))
    store = FakeStore()
    outcome = go(app, fetcher, store)
    assert outcome.status == "ok"
    assert (outcome.rows_in, outcome.rows_out) == (3, 3)
    assert outcome.note is None
    assert len(store.objects) == 3
    assert count(app, "raw_document") == 3
    assert sorted(r.url.path for r in net.requests) == [
        "/news/samplebay-restart",
        "/news/testville-fire",
        "/news/turnaround-roundup",
        "/rss",
    ]
    rows = app.execute(
        "SELECT url, source_id, published_tz, publication_date, prefilter_passed, headline"
        " FROM raw_document ORDER BY url"
    ).fetchall()
    by_url = {r["url"]: r for r in rows}
    fire = by_url["https://hp.example.com/news/testville-fire"]
    assert fire["source_id"] == "hp_news"
    assert fire["published_tz"] == "Etc/GMT-8"
    assert str(fire["publication_date"]) == "2026-11-10"
    assert fire["prefilter_passed"] is True
    assert fire["headline"] == "Testville Refinery crude unit shut after fire"
    undated = by_url["https://hp.example.com/news/turnaround-roundup"]
    assert undated["published_tz"] == "UTC"
    review = app.execute("SELECT reason, candidate FROM review_item").fetchall()
    assert [r["reason"] for r in review] == ["thin_page"]
    assert review[0]["candidate"]["images"] == 2


def test_second_run_makes_no_article_requests(app: Conn) -> None:
    pages = {"/news/a": html_response("article_handmade.html")}
    fetcher, net, _ = make_fetcher(site(rss("https://hp.example.com/news/a"), pages))
    store = FakeStore()
    assert go(app, fetcher, store).rows_out == 1
    before = len(net.requests)
    outcome = go(app, fetcher, store)
    assert outcome.rows_out == 0
    assert outcome.note == "known_url=1"
    assert [r.url.path for r in net.requests[before:]] == ["/rss"]


def test_same_text_at_a_new_url_is_a_known_hash(app: Conn) -> None:
    page = html_response("article_handmade.html")
    feed = rss("https://hp.example.com/news/a", "https://hp.example.com/news/b").replace(
        b"Item 1", b"Item 0"
    )
    fetcher, _, _ = make_fetcher(site(feed, {"/news/a": page, "/news/b": page}))
    store = FakeStore()
    outcome = go(app, fetcher, store, matcher=never)
    assert (outcome.rows_in, outcome.rows_out) == (2, 1)
    assert outcome.note == "known_hash=1"
    assert len(store.objects) == 1


def test_off_host_link_is_not_fetched(app: Conn) -> None:
    feed = rss("https://other.example.org/news/a")
    fetcher, net, _ = make_fetcher(site(feed, {"/news/a": html_response("article_handmade.html")}))
    outcome = go(app, fetcher, FakeStore())
    assert outcome.rows_out == 0
    assert outcome.note == "off_host=1"
    assert [r.url.path for r in net.requests] == ["/rss"]


def test_blocked_host_fails_the_run(app: Conn) -> None:
    feed = rss("https://hp.example.com/news/a", "https://hp.example.com/news/b")
    fetcher, net, _ = make_fetcher(site(feed, {"/news/a": httpx.Response(403)}))
    outcome = go(app, fetcher, FakeStore())
    assert outcome.status == "failed"
    assert outcome.note is not None
    assert outcome.note.startswith("HostBlocked")
    assert [r.url.path for r in net.requests] == ["/rss", "/news/a"]


def test_one_article_error_is_counted_and_the_run_continues(app: Conn) -> None:
    feed = rss("https://hp.example.com/news/a", "https://hp.example.com/news/b")
    pages = {"/news/b": html_response("article_handmade.html")}
    fetcher, _, _ = make_fetcher(site(feed, pages))
    outcome = go(app, fetcher, FakeStore())
    assert outcome.status == "ok"
    assert outcome.rows_out == 1
    assert outcome.note == "fetch_error=1"


def test_robots_disallowed_article_is_counted(app: Conn) -> None:
    feed = rss("https://hp.example.com/news/a")
    fetcher, net, _ = make_fetcher(
        site(feed, {}), robots=lambda r: httpx.Response(200, text="User-agent: *\nDisallow: /news/")
    )
    outcome = go(app, fetcher, FakeStore())
    assert outcome.note == "robots=1"
    assert [r.url.path for r in net.requests] == ["/rss"]


def test_non_html_reply_is_skipped(app: Conn) -> None:
    pdf = httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
    fetcher, _, _ = make_fetcher(site(rss("https://hp.example.com/news/a"), {"/news/a": pdf}))
    outcome = go(app, fetcher, FakeStore())
    assert outcome.note == "not_html=1"
    assert outcome.rows_out == 0


def test_empty_feed(app: Conn) -> None:
    fetcher, _, _ = make_fetcher(site(sample_bytes("rss_feed_empty_handmade.xml"), {}))
    outcome = go(app, fetcher, FakeStore())
    assert (outcome.status, outcome.rows_in, outcome.rows_out, outcome.note) == (
        "ok",
        0,
        0,
        "empty feed",
    )


def test_bad_items_are_counted_in_the_note(app: Conn) -> None:
    pages = {
        "/news/testville-fire": html_response("article_handmade.html"),
        "/news/samplebay-restart": html_response("article_no_date_handmade.html"),
    }
    feed = sample_bytes("rss_feed_bad_item_handmade.xml")
    fetcher, _, _ = make_fetcher(site(feed, pages))
    outcome = go(app, fetcher, FakeStore())
    assert (outcome.rows_in, outcome.rows_out) == (2, 2)
    assert outcome.note == "bad_item=1"


def test_feed_failures_fail_the_run(app: Conn) -> None:
    fetcher, _, _ = make_fetcher(site(b"<feed xmlns='http://www.w3.org/2005/Atom'/>", {}))
    outcome = go(app, fetcher, FakeStore())
    assert outcome.status == "failed"
    assert outcome.note == "FeedFormatError: feed is Atom, not RSS 2.0"
    fetcher, _, _ = make_fetcher(lambda r: httpx.Response(404))
    assert go(app, fetcher, FakeStore()).status == "failed"


def test_implausible_feed_date_falls_back_to_the_page_date(app: Conn) -> None:
    xml = (
        b"<rss><channel><item><title>Fire shut</title>"
        b"<link>https://hp.example.com/news/a</link>"
        b"<pubDate>Tue, 10 Nov 2030 14:00:00 GMT</pubDate></item></channel></rss>"
    )
    fetcher, _, _ = make_fetcher(site(xml, {"/news/a": html_response("article_handmade.html")}))
    go(app, fetcher, FakeStore())
    row = app.execute("SELECT published_at, published_tz FROM raw_document").fetchone()
    assert row is not None
    assert row["published_at"].year == 2026
    assert row["published_tz"] == "Etc/GMT-8"


def test_deadline_stops_fetching_and_says_so(app: Conn) -> None:
    feed = rss("https://hp.example.com/news/a", "https://hp.example.com/news/b")
    pages = {"/news/a": html_response("article_handmade.html")}
    inner = site(feed, pages)
    holder: list[Clock] = []

    def slow_feed(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rss":
            holder[0].t += 50.0
        return inner(request)

    fetcher, net, clock = make_fetcher(slow_feed)
    holder.append(clock)
    outcome = go(app, fetcher, FakeStore(), deadline=clock.t + 40.0)
    assert outcome.status == "ok"
    assert outcome.rows_out == 0
    assert outcome.note == "deadline after 0 documents"
    assert [r.url.path for r in net.requests] == ["/rss"]


def test_a_wait_that_would_pass_the_deadline_ends_the_loop(app: Conn) -> None:
    feed = rss("https://hp.example.com/news/a", "https://hp.example.com/news/b")
    pages = {"/news/a": html_response("article_handmade.html")}
    fetcher, net, clock = make_fetcher(site(feed, pages))
    outcome = go(app, fetcher, FakeStore(), deadline=clock.t + 6.0)
    assert outcome.status == "ok"
    assert outcome.note == "budget=1"
    assert [r.url.path for r in net.requests] == ["/rss"]


def test_at_most_fifty_articles_per_run(app: Conn) -> None:
    links = [f"https://hp.example.com/news/{n}" for n in range(55)]
    fetcher, net, _ = make_fetcher(site(rss(*links), {}))
    outcome = go(app, fetcher, FakeStore())
    assert outcome.rows_in == 55
    assert len([r for r in net.requests if r.url.path != "/rss"]) == 50
