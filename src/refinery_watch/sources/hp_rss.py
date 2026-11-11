"""Hydrocarbon Processing RSS adapter: feed items, then each article page."""

from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from refinery_watch.db.connect import Conn
from refinery_watch.objects import RawStore
from refinery_watch.sources.http import (
    Fetcher,
    FetchError,
    HostBlocked,
    RobotsDisallowed,
    strip_tracking,
)
from refinery_watch.sources.pages import fetch_page
from refinery_watch.sources.published import Published, parse_rfc822, plausible
from refinery_watch.sources.store import (
    NewDocument,
    RunOutcome,
    Stored,
    TermMatcher,
    flag_thin_page,
    store_document,
    url_known,
)
from refinery_watch.sources.text import content_sha256, is_thin

HpSource = Literal["hp_news", "hp_refining"]
MAX_ARTICLES: Final = 50
FEED_ACCEPT: Final = "application/rss+xml, application/xml"


@dataclass(frozen=True, slots=True)
class FeedItem:
    title: str
    link: str
    guid: str | None
    published: Published | None


class FeedFormatError(ValueError):
    """The feed is not RSS 2.0 or not well-formed XML."""


def _text(item: Element, tag: str) -> str | None:
    found = item.find(tag)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def parse_feed(xml: bytes) -> tuple[list[FeedItem], list[str]]:
    try:
        root = SafeET.fromstring(xml)
    except (SafeET.ParseError, DefusedXmlException):
        raise FeedFormatError("feed is not well-formed XML") from None
    if root.tag.endswith("}feed") or root.tag == "feed":
        raise FeedFormatError("feed is Atom, not RSS 2.0")
    if root.tag != "rss":
        raise FeedFormatError("feed root is not rss")
    items: list[FeedItem] = []
    reasons: list[str] = []
    for n, item in enumerate(root.iterfind("channel/item"), start=1):
        title = _text(item, "title")
        link = _text(item, "link")
        if title is None or link is None:
            reasons.append(f"item {n}: missing {'title' if title is None else 'link'}")
            continue
        raw_date = _text(item, "pubDate")
        items.append(
            FeedItem(
                title=title,
                link=link,
                guid=_text(item, "guid"),
                published=parse_rfc822(raw_date) if raw_date else None,
            )
        )
    return items, reasons


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def run(
    conn: Conn,
    fetcher: Fetcher,
    store: RawStore,
    matcher: TermMatcher,
    source_id: HpSource,
    run_id: int,
    feed_url: str,
    deadline: float,
) -> RunOutcome:
    try:
        feed = fetcher.get(feed_url, accept=FEED_ACCEPT)
        items, reasons = parse_feed(feed.body)
    except (FetchError, FeedFormatError) as exc:
        return RunOutcome("failed", 0, 0, f"{type(exc).__name__}: {exc}")
    if not items and not reasons:
        return RunOutcome("ok", 0, 0, "empty feed")
    counts: dict[str, int] = {}

    def count(name: str, n: int = 1) -> None:
        counts[name] = counts.get(name, 0) + n

    count("bad_item", len(reasons))
    stored = 0
    feed_host = _host(feed_url)
    for item in items[:MAX_ARTICLES]:
        link = strip_tracking(item.link)
        if _host(link) != feed_host:
            count("off_host")
            continue
        if url_known(conn, [link]):
            count("known_url")
            continue
        if fetcher.monotonic() >= deadline:
            count("deadline")
            break
        try:
            fetched = fetch_page(fetcher, link)
        except HostBlocked as exc:
            return RunOutcome("failed", len(items), stored, f"HostBlocked: {exc}")
        except RobotsDisallowed:
            count("robots")
            continue
        except FetchError:
            count("fetch_error")
            continue
        if fetched is None:
            count("not_html")
            continue
        page = fetched.page
        published = item.published
        if published is None or not plausible(published, fetched.result.fetched_at):
            published = page.published
            if published is not None and not plausible(published, fetched.result.fetched_at):
                published = None
        result = store_document(
            conn,
            store,
            run_id,
            NewDocument(
                source_id=source_id,
                url=link,
                original=fetched.result.body,
                ext="html",
                content_type="text/html",
                content_sha256=content_sha256(item.title, page.text),
                headline=item.title,
                body_chars=len(page.text),
                fetched_at=fetched.result.fetched_at,
                published=published,
                prefilter_terms=tuple(matcher(item.title + "\n" + page.text)),
            ),
        )
        if isinstance(result, Stored):
            stored += 1
            if is_thin(page):
                flag_thin_page(conn, result.raw_document_id, link, len(page.text), page.image_count)
        else:
            count("known_hash")
    notes = [f"{name}={n}" for name, n in counts.items() if n and name != "deadline"]
    if counts.get("deadline"):
        notes.append(f"deadline after {stored} documents")
    return RunOutcome("ok", len(items), stored, "; ".join(notes) or None)
