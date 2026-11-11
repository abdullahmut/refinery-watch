"""Fetches one article page and turns it into text. Shared by the HP and GDELT adapters."""

from collections.abc import Sequence
from dataclasses import dataclass

from refinery_watch.sources.http import Fetcher, FetchResult, host_key
from refinery_watch.sources.text import PageText, decode_html, page_text


def host_of(url: str) -> str:
    """The host a URL points at, in the form the fetcher keys its rules on."""
    return host_key(url)


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


class Tally:
    """Counts of what a run skipped, written to the job note."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def add(self, name: str, n: int = 1) -> None:
        if n:
            self.counts[name] = self.counts.get(name, 0) + n

    def get(self, name: str) -> int:
        return self.counts.get(name, 0)

    def note(self, stored: int, extra: Sequence[str] = ()) -> str | None:
        parts = [f"{k}={v}" for k, v in self.counts.items() if k != "deadline"]
        parts.extend(extra)
        if self.get("deadline"):
            parts.append(f"deadline after {stored} documents")
        return "; ".join(parts) or None
