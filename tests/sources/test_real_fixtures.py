"""Tests on real fetched files. They skip when the files are absent."""

import json
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from refinery_watch.sources.gdelt_doc import parse_artlist
from refinery_watch.sources.hp_rss import parse_feed
from refinery_watch.sources.text import decode_html, is_thin, page_text

Pick = Callable[[str], Path]

HP_HOST = "www.hydrocarbonprocessing.com"


@pytest.mark.parametrize("name", ["hp_news", "hp_refining"])
def test_real_feed_parses_cleanly(real_fixture: Pick, name: str) -> None:
    items, reasons = parse_feed(real_fixture(f"hp_rss/{name}_*.xml").read_bytes())
    assert len(items) >= 1
    assert reasons == []
    for item in items:
        parts = urlsplit(item.link)
        assert parts.scheme == "https"
        assert parts.hostname == HP_HOST
        assert item.published is not None
        assert item.published.zone_stated


def test_real_empty_feed(real_fixture: Pick) -> None:
    path = real_fixture("hp_rss/hp_news_empty_derived_*.xml")
    assert parse_feed(path.read_bytes()) == ([], [])


def test_real_feed_with_one_item_missing_a_title(real_fixture: Pick) -> None:
    derived = real_fixture("hp_rss/hp_news_missing_title_derived_*.xml")
    source = real_fixture("hp_rss/hp_news_*.xml")
    items, reasons = parse_feed(derived.read_bytes())
    full, _ = parse_feed(source.read_bytes())
    assert len(reasons) == 1
    assert len(items) == len(full) - 1


def test_real_article_text(real_fixture: Pick) -> None:
    html = decode_html(real_fixture("gdelt/hp_article_normal_*.html").read_bytes(), None)
    page = page_text(html)
    assert len(page.text) >= 600
    assert page.title
    assert page.published is not None
    assert not is_thin(page)


def test_real_thin_article(real_fixture: Pick) -> None:
    html = decode_html(real_fixture("gdelt/hp_article_thin_*.html").read_bytes(), None)
    assert is_thin(page_text(html))


def test_real_artlist(real_fixture: Pick) -> None:
    leads = parse_artlist(real_fixture("gdelt/gdelt_doc_artlist_*.json").read_bytes())
    assert len(leads) >= 1
    for lead in leads:
        assert lead.seen_at.utcoffset() is not None
        assert lead.language == "English"


def test_real_empty_artlist(real_fixture: Pick) -> None:
    assert parse_artlist(real_fixture("gdelt/gdelt_doc_empty_*.json").read_bytes()) == []


def api_key_values(node: object) -> list[object]:
    found: list[object] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "api_key":
                found.append(value)
            found.extend(api_key_values(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(api_key_values(item))
    return found


@pytest.mark.parametrize("pattern", ["eia/eia_v2_wiup_route_*.json", "eia/eia_v2_wiup_data_*.json"])
def test_real_eia_files_hold_no_key(real_fixture: Pick, pattern: str) -> None:
    data = json.loads(real_fixture(pattern).read_text(encoding="utf-8"))
    assert all(value == "REDACTED" for value in api_key_values(data))
