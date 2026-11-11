"""Fetches one article page and turns it into text. Shared by the HP and GDELT adapters."""

from dataclasses import dataclass

from refinery_watch.sources.http import Fetcher, FetchResult
from refinery_watch.sources.text import PageText, decode_html, page_text


@dataclass(frozen=True, slots=True)
class FetchedPage:
    result: FetchResult
    page: PageText


def fetch_page(fetcher: Fetcher, url: str) -> FetchedPage | None:
    """Returns None when the reply is not HTML. Fetch errors propagate."""
    result = fetcher.get(url, accept="text/html")
    if result.content_type != "text/html":
        return None
    return FetchedPage(result, page_text(decode_html(result.body, result.charset)))
