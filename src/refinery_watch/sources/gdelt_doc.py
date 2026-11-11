"""GDELT DOC 2.0 adapter. A hit is a lead: the article page is fetched and read."""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from refinery_watch.db.connect import Conn
from refinery_watch.objects import RawStore
from refinery_watch.sources.clean import clean_text
from refinery_watch.sources.http import (
    BudgetExceeded,
    Fetcher,
    FetchError,
    HostBlocked,
    RobotsDisallowed,
    host_key,
    strip_tracking,
)
from refinery_watch.sources.pages import Tally, fetch_page
from refinery_watch.sources.published import plausible
from refinery_watch.sources.store import (
    NewDocument,
    RunOutcome,
    Stored,
    flag_thin_page,
    store_document,
    url_known,
)
from refinery_watch.sources.text import content_sha256, is_thin
from refinery_watch.terms import TermMatcher

log = logging.getLogger(__name__)
SOURCE_ID: Final = "gdelt_doc"
QUERY: Final = (
    "refinery (fire OR outage OR shutdown OR restart OR CDU OR FCC OR hydrocracker) "
    "sourcelang:english"
)
MAX_RECORDS: Final = 75
MAX_LEAD_AGE: Final = timedelta(days=14)
SEEN_FORMAT: Final = "%Y%m%dT%H%M%SZ"
ERROR_TEXT_CHARS: Final = 200


@dataclass(frozen=True, slots=True)
class Lead:
    url: str
    title: str
    seen_at: datetime
    domain: str
    language: str


class GdeltResponseError(ValueError):
    """GDELT answered with text that is not an article list."""


def build_params(interval_min: int) -> list[tuple[str, str]]:
    return [
        ("query", QUERY),
        ("mode", "ArtList"),
        ("format", "json"),
        ("maxrecords", str(MAX_RECORDS)),
        ("sort", "DateDesc"),
        ("timespan", f"{2 * interval_min}min"),
    ]


def _lead(entry: object) -> Lead | None:
    if not isinstance(entry, dict):
        return None
    url = entry.get("url")
    seen = entry.get("seendate")
    if not isinstance(url, str) or not url or not isinstance(seen, str):
        return None
    try:
        seen_at = datetime.strptime(seen, SEEN_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None
    return Lead(
        url=url,
        title=str(entry.get("title") or ""),
        seen_at=seen_at,
        domain=str(entry.get("domain") or ""),
        language=str(entry.get("language") or ""),
    )


def parse_artlist(body: bytes) -> list[Lead]:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    try:
        data = json.loads(text, strict=False)
    except ValueError:
        raise GdeltResponseError(clean_text(text)[:ERROR_TEXT_CHARS]) from None
    if not isinstance(data, dict):
        raise GdeltResponseError(clean_text(text)[:ERROR_TEXT_CHARS])
    if not data:
        return []
    articles = data.get("articles")
    if not isinstance(articles, list):
        raise GdeltResponseError(clean_text(text)[:ERROR_TEXT_CHARS])
    return [lead for lead in map(_lead, articles) if lead is not None]


def run(
    conn: Conn,
    fetcher: Fetcher,
    store: RawStore,
    matcher: TermMatcher,
    run_id: int,
    feed_url: str,
    interval_min: int,
    deadline: float,
) -> RunOutcome:
    fetcher.set_deadline(deadline)
    try:
        reply = fetcher.get(feed_url, params=build_params(interval_min), accept="application/json")
        leads = parse_artlist(reply.body)
    except (FetchError, GdeltResponseError) as exc:
        return RunOutcome("failed", 0, 0, f"{type(exc).__name__}: {exc}")
    tally = Tally()
    stored = 0
    api_host = host_key(feed_url)
    blocked: list[str] = []
    seen_urls: set[str] = set()
    english = [lead for lead in leads if lead.language.casefold() == "english"]
    tally.add("not_english", len(leads) - len(english))
    for lead in english[:MAX_RECORDS]:
        url = strip_tracking(lead.url)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        if url_known(conn, [url]):
            tally.add("known_url")
            continue
        if fetcher.monotonic() >= deadline:
            tally.add("deadline")
            break
        host = host_key(url)
        try:
            fetched = fetch_page(fetcher, url)
        except HostBlocked as exc:
            if host == api_host or exc.host == api_host:
                return RunOutcome("failed", len(leads), stored, f"HostBlocked: {exc}")
            if host not in blocked:
                blocked.append(host)
            tally.add("blocked_host")
            continue
        except BudgetExceeded:
            tally.add("budget")
            continue
        except RobotsDisallowed:
            tally.add("robots")
            continue
        except FetchError:
            tally.add("fetch_error")
            continue
        except Exception:
            # One page that breaks the parser must not end the run.
            log.exception("page failed to parse")
            tally.add("parse_error")
            continue
        if fetched is None:
            tally.add("not_html")
            continue
        page = fetched.page
        fetched_at = fetched.result.fetched_at
        published = page.published
        if published is None:
            tally.add("no_pubdate")
            continue
        if not plausible(published, fetched_at):
            tally.add("future_pubdate")
            continue
        if published.at < fetched_at - MAX_LEAD_AGE:
            tally.add("stale")
            continue
        headline = page.title or lead.title or None
        result = store_document(
            conn,
            store,
            run_id,
            NewDocument(
                source_id=SOURCE_ID,
                url=url,
                original=fetched.result.body,
                ext="html",
                content_type="text/html",
                content_sha256=content_sha256(page.title, page.text),
                headline=headline,
                body_chars=len(page.text),
                fetched_at=fetched_at,
                published=published,
                prefilter_terms=tuple(matcher((headline or "") + "\n" + page.text)),
            ),
        )
        if isinstance(result, Stored):
            stored += 1
            if is_thin(page):
                flag_thin_page(conn, result.raw_document_id, url, len(page.text), page.image_count)
        else:
            tally.add("known_hash")
    extra = [f"blocked hosts: {', '.join(blocked)}"] if blocked else []
    return RunOutcome("ok", len(leads), stored, tally.note(stored, extra))
