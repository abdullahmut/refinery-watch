from datetime import UTC, datetime

import pytest
from tests.sources.helpers import sample_bytes

from refinery_watch.sources.gdelt_doc import (
    GdeltResponseError,
    build_params,
    parse_artlist,
)


def test_handmade_leads() -> None:
    leads = parse_artlist(sample_bytes("gdelt_artlist_handmade.json"))
    assert len(leads) == 4
    first = leads[0]
    assert first.url == "https://news.example.com/a?utm_source=wire"
    assert first.title == "Testville refinery unit shut after fire"
    assert first.seen_at == datetime(2026, 11, 10, 14, 30, tzinfo=UTC)
    assert first.domain == "news.example.com"
    assert first.language == "English"
    assert leads[3].language == "Spanish"


def test_empty_bodies_give_no_leads() -> None:
    assert parse_artlist(b"") == []
    assert parse_artlist(b"  \n") == []
    assert parse_artlist(b"{}") == []
    assert parse_artlist(b'{"articles": []}') == []


def test_rate_limit_text_raises_with_the_text() -> None:
    with pytest.raises(GdeltResponseError, match="Please limit requests to one every 5 seconds"):
        parse_artlist(b"Please limit requests to one every 5 seconds")


def test_error_text_is_cut_to_200_characters() -> None:
    with pytest.raises(GdeltResponseError) as info:
        parse_artlist(b"x" * 500)
    assert len(str(info.value)) == 200


@pytest.mark.parametrize("body", [b"[1, 2]", b'{"other": 1}', b'{"articles": "no"}'])
def test_unexpected_json_shapes_raise(body: bytes) -> None:
    with pytest.raises(GdeltResponseError):
        parse_artlist(body)


def test_bad_entries_are_dropped() -> None:
    body = (
        b'{"articles": [1, {"url": ""}, {"url": "https://a.example.com/x", "seendate": "bad"},'
        b' {"url": "https://a.example.com/y", "seendate": "20261110T143000Z"}]}'
    )
    leads = parse_artlist(body)
    assert [lead.url for lead in leads] == ["https://a.example.com/y"]
    assert leads[0].title == ""


def test_control_characters_in_titles_are_tolerated() -> None:
    body = (
        b'{"articles": [{"url": "https://a.example.com/y", "title": "a\tb\x01c",'
        b' "seendate": "20261110T143000Z"}]}'
    )
    assert parse_artlist(body)[0].title == "a\tb\x01c"


def test_build_params() -> None:
    params = dict(build_params(60))
    assert "sourcelang:english" in params["query"]
    assert params["mode"] == "ArtList"
    assert params["format"] == "json"
    assert params["timespan"] == "120min"
    assert params["maxrecords"] == "75"
    assert params["sort"] == "DateDesc"
