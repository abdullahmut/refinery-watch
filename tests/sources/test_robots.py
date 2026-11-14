import httpx
import pytest
from tests.sources.helpers import make_fetcher

from refinery_watch.sources.http import RobotsDisallowed
from refinery_watch.sources.robots import parse_robots

TOKEN = "refinery-watch"  # noqa: S105


def allows(text: str, path: str) -> bool:
    return parse_robots(text, TOKEN).allows(path)


def test_longest_match_beats_a_shorter_allow() -> None:
    text = "User-agent: *\nAllow: /\nDisallow: /admin/\n"
    assert not allows(text, "/admin/x")
    assert allows(text, "/news")


def test_longer_allow_beats_a_shorter_disallow() -> None:
    text = "User-agent: *\nDisallow: /news/\nAllow: /news/public/\n"
    assert allows(text, "/news/public/a")
    assert not allows(text, "/news/private/a")


def test_allow_wins_a_tie() -> None:
    text = "User-agent: *\nDisallow: /a\nAllow: /a\n"
    assert allows(text, "/a/b")
    text = "User-agent: *\nAllow: /a\nDisallow: /a\n"
    assert allows(text, "/a/b")


def test_star_wildcard_and_query_matching() -> None:
    text = "User-agent: *\nDisallow: /*?print=\n"
    assert not allows(text, "/news/a?print=1")
    assert allows(text, "/news/a?page=1")


def test_dollar_anchors_the_end() -> None:
    text = "User-agent: *\nDisallow: /*.pdf$\n"
    assert not allows(text, "/files/a.pdf")
    assert allows(text, "/files/a.pdf?x=1")
    assert allows(text, "/files/a.pdfx")
    assert not allows("User-agent: *\nDisallow: /exact$\n", "/exact")
    assert allows("User-agent: *\nDisallow: /exact$\n", "/exact/more")


def test_wildcards_in_the_middle() -> None:
    text = "User-agent: *\nDisallow: /a*b*c\n"
    assert not allows(text, "/aXXbYYc")
    assert allows(text, "/aXXcYYb")


def test_many_wildcards_do_not_blow_up() -> None:
    text = "User-agent: *\nDisallow: " + "/*a" * 40 + "b\n"
    assert allows(text, "/" + "a" * 5000)


def test_empty_disallow_allows_everything() -> None:
    assert allows("User-agent: *\nDisallow:\n", "/anything")


def test_no_rules_and_no_matching_group_allow() -> None:
    assert allows("", "/x")
    assert allows("User-agent: otherbot\nDisallow: /\n", "/x")


def test_our_group_beats_the_star_group() -> None:
    text = "User-agent: *\nDisallow: /\n\nUser-agent: Refinery-Watch\nAllow: /\n"
    assert allows(text, "/x")
    text = "User-agent: *\nAllow: /\n\nUser-agent: refinery-watch\nDisallow: /\n"
    assert not allows(text, "/x")


def test_consecutive_agent_lines_share_a_group() -> None:
    text = "User-agent: other\nUser-agent: refinery-watch\nDisallow: /x\n"
    assert not allows(text, "/x")


def test_comments_bom_and_unknown_lines() -> None:
    text = "﻿User-agent: * # everyone\nSitemap: https://example.com/s.xml\nDisallow: /p # no\n"
    assert not allows(text, "/p")


def test_percent_encoding_case_is_normalised() -> None:
    text = "User-agent: *\nDisallow: /a%2fb\n"
    assert not allows(text, "/a%2Fb")


def test_crawl_delay() -> None:
    assert parse_robots("User-agent: *\nCrawl-delay: 12\n", TOKEN).crawl_delay == 12.0
    assert parse_robots("User-agent: *\nCrawl-delay: x\n", TOKEN).crawl_delay is None
    assert parse_robots("User-agent: *\nDisallow: /\n", TOKEN).crawl_delay is None


def test_rules_before_any_group_are_ignored() -> None:
    assert allows("Disallow: /\nUser-agent: *\nAllow: /\n", "/x")


@pytest.mark.parametrize(
    ("robots", "path"),
    [
        ("User-agent: *\nAllow: /\nDisallow: /admin/\n", "/admin/x"),
        ("User-agent: *\nDisallow: /*?print=\n", "/news/a?print=1"),
        ("User-agent: *\nDisallow: /*.pdf$\n", "/files/a.pdf"),
    ],
)
def test_fetcher_honours_rfc_9309_rules(robots: str, path: str) -> None:
    fetcher, net, _ = make_fetcher(
        lambda r: httpx.Response(200, content=b"x"),
        robots=lambda r: httpx.Response(200, text=robots),
    )
    with pytest.raises(RobotsDisallowed):
        fetcher.get("https://a.example.com" + path)
    assert net.requests == []
