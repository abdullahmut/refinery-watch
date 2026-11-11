from datetime import UTC, datetime

import pytest
from tests.sources.helpers import sample_bytes

from refinery_watch.sources.hp_rss import FeedFormatError, parse_feed
from refinery_watch.sources.published import Published

BOMB = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>
<rss version="2.0"><channel><item><title>&lol2;</title><link>https://hp.example.com/a</link></item></channel></rss>"""

EXTERNAL = b"""<?xml version="1.0"?>
<!DOCTYPE x [<!ENTITY ext SYSTEM "file:///etc/passwd">]>
<rss version="2.0"><channel><item><title>&ext;</title><link>https://hp.example.com/a</link></item></channel></rss>"""


def test_three_handmade_items() -> None:
    items, reasons = parse_feed(sample_bytes("rss_feed_handmade.xml"))
    assert reasons == []
    assert [i.title for i in items] == [
        "Testville Refinery crude unit shut after fire",
        "Samplebay unit restart planned",
        "Turnaround round-up for November",
    ]
    assert items[0].link.startswith("https://hp.example.com/news/testville-fire")
    assert items[0].guid == "hand-1"
    assert items[0].published == Published(
        datetime(2026, 11, 10, 12, 0, tzinfo=UTC), "Etc/GMT-8", True
    )
    assert items[1].published == Published(datetime(2026, 11, 10, 14, 0, tzinfo=UTC), "UTC", True)
    assert items[2].published is None


def test_item_without_title_is_skipped_with_a_reason() -> None:
    items, reasons = parse_feed(sample_bytes("rss_feed_bad_item_handmade.xml"))
    assert len(items) == 2
    assert reasons == ["item 2: missing title"]


def test_item_without_link_is_skipped() -> None:
    xml = b"<rss><channel><item><title>t</title></item></channel></rss>"
    assert parse_feed(xml) == ([], ["item 1: missing link"])


def test_empty_feed() -> None:
    assert parse_feed(sample_bytes("rss_feed_empty_handmade.xml")) == ([], [])


def test_unparseable_date_gives_none() -> None:
    xml = (
        b"<rss><channel><item><title>t</title><link>https://hp.example.com/a</link>"
        b"<pubDate>soon</pubDate></item></channel></rss>"
    )
    items, _ = parse_feed(xml)
    assert items[0].published is None


@pytest.mark.parametrize(
    "xml",
    [sample_bytes("atom_feed_handmade.xml"), b"<rss><channel>", b"", BOMB, EXTERNAL, b"<html/>"],
    ids=["atom", "broken", "empty", "entity_bomb", "external_entity", "not_rss"],
)
def test_bad_feeds_raise(xml: bytes) -> None:
    with pytest.raises(FeedFormatError):
        parse_feed(xml)
