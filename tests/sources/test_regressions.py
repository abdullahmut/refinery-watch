"""Regression tests for defects found after the first adapters build. Offline only."""

import httpx
import pytest
from tests.sources.helpers import make_fetcher

from refinery_watch.sources.hp_rss import parse_feed
from refinery_watch.sources.published import parse_iso8601, parse_rfc822
from refinery_watch.sources.text import decode_html, page_text


@pytest.mark.parametrize("value", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:00:00-05:00"])
def test_iso_date_out_of_utc_range_is_none(value: str) -> None:
    assert parse_iso8601(value) is None


def test_rfc822_date_out_of_utc_range_is_none() -> None:
    assert parse_rfc822("Fri, 31 Dec 9999 23:00:00 -0500") is None


def test_feed_item_with_placeholder_date_does_not_break_the_feed() -> None:
    feed = (
        b"<rss><channel>"
        b"<item><title>a</title><link>https://hp.example.com/news/a</link>"
        b"<pubDate>Fri, 31 Dec 9999 23:00:00 -0500</pubDate></item>"
        b"<item><title>b</title><link>https://hp.example.com/news/b</link></item>"
        b"</channel></rss>"
    )
    items, _ = parse_feed(feed)
    assert [i.link for i in items] == [
        "https://hp.example.com/news/a",
        "https://hp.example.com/news/b",
    ]
    assert items[0].published is None


def test_page_with_placeholder_meta_date_still_parses() -> None:
    html = (
        '<meta property="article:published_time" content="0001-01-01T00:00:00+01:00">'
        "<article><p>text</p></article>"
    )
    page = page_text(html)
    assert page.text == "text"
    assert page.published is None


@pytest.mark.parametrize("charset", ["base64", "rot13", "hex", "punycode", "no-such-codec"])
def test_decode_html_ignores_non_text_codecs(charset: str) -> None:
    assert decode_html("<p>café</p>".encode(), charset) == "<p>café</p>"


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("shift_jis", "精製"),
        ("gb18030", "精炼"),
        ("euc-kr", "정유"),
        ("utf-16", "café"),
        ("windows-1252", "café"),
        ("iso-8859-1", "café"),
    ],
)
def test_allowed_charsets_decode(name: str, text: str) -> None:
    assert decode_html(text.encode(name), name) == text


def test_meta_charset_to_a_non_text_codec_is_ignored() -> None:
    body = b'<meta charset="base64"><p>plain</p>'
    assert "plain" in decode_html(body, None)


def test_deep_json_ld_is_ignored() -> None:
    html = (
        '<script type="application/ld+json">' + "[" * 50000 + "]" * 50000 + "</script>"
        "<article><p>text</p></article>"
    )
    assert page_text(html).text == "text"


def test_nested_json_ld_beyond_the_depth_cap_gives_no_date() -> None:
    nested = '{"datePublished": "2026-11-10"}'
    for _ in range(12):
        nested = '{"@graph": [' + nested + "]}"
    html = f'<script type="application/ld+json">{nested}</script><p>x</p>'
    assert page_text(html).published is None


def test_oversized_json_ld_is_ignored() -> None:
    padding = " " * 150_000
    html = (
        '<script type="application/ld+json">{"datePublished": "2026-11-10"'
        + padding
        + "}</script><p>x</p>"
    )
    assert page_text(html).published is None


def test_ast_is_read_as_no_stated_zone() -> None:
    p = parse_rfc822("Tue, 10 Nov 2026 14:00:00 AST")
    assert p is not None
    assert p.tz is None
    assert not p.zone_stated
    assert p.at.hour == 14


def test_est_is_still_a_stated_zone() -> None:
    p = parse_rfc822("Tue, 10 Nov 2026 14:00:00 EST")
    assert p is not None
    assert p.zone_stated


@pytest.mark.parametrize(
    "value", ["Mon, 01 Jan 0001 00:00:00 +0000", "Mon, 01 Jan 1899 00:00:00 +0000"]
)
def test_four_digit_year_below_1900_is_refused(value: str) -> None:
    assert parse_rfc822(value) is None


def test_iso_year_below_1900_is_refused() -> None:
    assert parse_iso8601("1899-12-31") is None
    assert parse_iso8601("1900-01-01") is not None


def test_redirect_does_not_resend_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a.example.com":
            return httpx.Response(302, headers={"location": "https://b.example.com/y"})
        return httpx.Response(200, content=b"ok")

    fetcher, net, _ = make_fetcher(handler)
    fetcher.get("https://a.example.com/x", params=[("api_key", "SECRET123")])
    assert "SECRET123" in str(net.requests[0].url)
    assert "SECRET123" not in str(net.requests[-1].url)
    assert len(net.requests) == 2
