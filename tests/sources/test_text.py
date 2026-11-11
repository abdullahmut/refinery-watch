from datetime import UTC, datetime
from pathlib import Path

from refinery_watch.sources.text import (
    THIN_CHARS,
    PageText,
    content_sha256,
    decode_html,
    is_thin,
    normalise_text,
    page_text,
)

HANDMADE = Path(__file__).resolve().parents[1] / "fixtures" / "sources" / "handmade"


def sample(name: str) -> str:
    return (HANDMADE / name).read_text(encoding="utf-8")


def test_article_keeps_article_text_and_drops_the_rest() -> None:
    page = page_text(sample("article_handmade.html"))
    assert "Test Oil Co shut the main crude distillation unit" in page.text
    assert "We are working to bring the unit back safely" in page.text
    for dropped in ("NAVTEXT", "HEADERTEXT", "FOOTERTEXT", "ASIDETEXT", "SCRIPTTEXT", "color: red"):
        assert dropped not in page.text
    assert len(page.text) >= THIN_CHARS


def test_article_title_date_and_images() -> None:
    page = page_text(sample("article_handmade.html"))
    assert page.title == "Testville Refinery crude unit shut after fire"
    assert page.image_count == 2
    assert page.published is not None
    assert page.published.at == datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    assert page.published.tz == "Etc/GMT-8"
    assert not is_thin(page)


def test_thin_page_is_thin() -> None:
    page = page_text(sample("article_thin_handmade.html"))
    assert len(page.text) < THIN_CHARS
    assert page.image_count == 2
    assert is_thin(page)
    assert page.published is not None
    assert page.published.tz == "UTC"


def test_no_date_page_uses_main_and_has_no_date() -> None:
    page = page_text(sample("article_no_date_handmade.html"))
    assert page.published is None
    assert page.title == "Samplebay unit restart planned"
    assert "plans to restart its hydrocracker" in page.text


def test_og_published_time_beats_json_ld() -> None:
    html = (
        '<html><head><meta property="article:published_time" content="2026-11-01T01:00:00Z">'
        '<script type="application/ld+json">{"datePublished": "2026-11-02T01:00:00Z"}</script>'
        "</head><body><p>x</p></body></html>"
    )
    published = page_text(html).published
    assert published is not None
    assert published.at.day == 1


def test_json_ld_graph_and_bad_json() -> None:
    graph = (
        '<script type="application/ld+json">'
        '{"@graph": [{"@type": "WebSite"}, {"datePublished": "2026-11-03"}]}</script>'
    )
    published = page_text(f"<html><head>{graph}</head><body><p>x</p></body></html>").published
    assert published is not None
    assert published.at.day == 3
    assert not published.zone_stated
    bad = '<script type="application/ld+json">{not json</script>'
    assert page_text(f"<html><head>{bad}</head><body><p>x</p></body></html>").published is None


def test_date_sources_in_order() -> None:
    body = "<body><p>x</p></body>"
    itemprop = '<meta itemprop="datePublished" content="2026-11-04">'
    name = '<meta name="DC.date" content="2026-11-05">'
    assert page_text(f"<head>{itemprop}{name}</head>{body}").published is not None
    only_name = page_text(f"<head>{name}</head>{body}").published
    assert only_name is not None
    assert only_name.at.day == 5
    time_tag = (
        '<body><article><time datetime="2026-11-06T10:00:00Z">t</time><p>x</p></article></body>'
    )
    from_time = page_text(time_tag).published
    assert from_time is not None
    assert from_time.at.day == 6


def test_unparseable_candidate_falls_through() -> None:
    html = (
        '<head><meta property="article:published_time" content="yesterday">'
        '<meta name="date" content="2026-11-05"></head><body><p>x</p></body>'
    )
    published = page_text(html).published
    assert published is not None
    assert published.at.day == 5


def test_title_falls_back_to_title_tag_then_h1() -> None:
    assert page_text("<title> A  title </title><p>x</p>").title == "A title"
    assert page_text("<body><h1>Heading</h1><p>x</p></body>").title == "Heading"
    assert page_text("<body><p>x</p></body>").title is None


def test_body_used_when_no_article_or_main() -> None:
    page = page_text(
        "<body><p>one</p><ul><li>two</li></ul><table><tr><td>three</td></tr></table></body>"
    )
    assert page.text == "one\ntwo\nthree"


def test_images_in_nav_are_not_counted() -> None:
    page = page_text("<body><nav><img src='a'></nav><p>text</p><img src='b'></body>")
    assert page.image_count == 1


def test_only_first_article_is_used() -> None:
    page = page_text("<body><article><p>first</p></article><article><p>second</p></article></body>")
    assert page.text == "first"


def test_unclosed_and_odd_markup_does_not_raise() -> None:
    page = page_text("<body><p>open <b>bold<p>next</i></div><br/><p>a")
    assert "open bold" in page.text


def test_is_thin_boundaries() -> None:
    assert is_thin(PageText(None, "x" * 599, 1, None))
    assert not is_thin(PageText(None, "x" * 600, 1, None))
    assert not is_thin(PageText(None, "x" * 100, 0, None))


def test_normalise_text_is_idempotent() -> None:
    raw = "  Café   one \n\n\t two  \n   \nthree"
    once = normalise_text(raw)
    assert once == "Café one\ntwo\nthree"
    assert normalise_text(once) == once


def test_hash_ignores_script_nav_and_whitespace() -> None:
    a = page_text(sample("article_handmade.html"))
    noisy = (
        sample("article_handmade.html")
        .replace("NAVTEXT Home", "NAVTEXT Other")
        .replace("<p>", "<p>  \n ")
    )
    b = page_text(noisy)
    assert content_sha256(a.title, a.text) == content_sha256(b.title, b.text)


def test_hash_changes_with_one_word() -> None:
    a = page_text(sample("article_handmade.html"))
    changed = page_text(
        sample("article_handmade.html").replace("Nobody was hurt", "Nobody was harmed")
    )
    assert content_sha256(a.title, a.text) != content_sha256(changed.title, changed.text)
    assert len(content_sha256(None, "x")) == 64


def test_decode_uses_meta_charset_without_header() -> None:
    html = '<html><head><meta charset="windows-1252"></head><body>caf\xe9</body></html>'
    assert "café" in decode_html(html.encode("latin-1"), None)


def test_decode_header_charset_wins_and_bad_names_fall_back() -> None:
    assert decode_html("café".encode("utf-16"), "utf-16") == "café"
    assert decode_html(b"caf\xc3\xa9", "no-such-codec") == "café"
    assert decode_html(b"caf\xff", None) == "caf�"
