"""HTML to text with the standard library, and the content hash taken over that text."""

import codecs
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Final

from refinery_watch.sources.published import Published, parse_iso8601

THIN_CHARS: Final = 600
SKIP_TAGS: Final = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "iframe",
        "nav",
        "header",
        "footer",
        "aside",
        "form",
    }
)
BLOCK_TAGS: Final = frozenset({"p", "h1", "h2", "h3", "h4", "li", "blockquote", "td"})
DATE_META_NAMES: Final = frozenset(
    {"pubdate", "publishdate", "date", "dc.date", "parsely-pub-date", "sailthru.date"}
)
META_SNIFF_BYTES: Final = 2048
JSON_LD_MAX_CHARS: Final = 100_000
JSON_LD_MAX_DEPTH: Final = 8
ALLOWED_CODECS: Final = frozenset(
    codecs.lookup(name).name
    for name in ("utf-8", "iso-8859-1", "windows-1252", "utf-16", "shift_jis", "gb18030", "euc-kr")
)
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.I)
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class PageText:
    title: str | None
    text: str
    image_count: int
    published: Published | None


def normalise_text(text: str) -> str:
    lines = (
        _SPACE.sub(" ", line).strip() for line in unicodedata.normalize("NFC", text).split("\n")
    )
    return "\n".join(line for line in lines if line)


def content_sha256(headline: str | None, text: str) -> str:
    joined = normalise_text(f"{headline or ''}\n{text}")
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def is_thin(page: PageText) -> bool:
    return len(page.text) < THIN_CHARS and page.image_count > 0


def _codec(name: str) -> str | None:
    """The codec name when it is on the allow-list. Other codecs are not text."""
    try:
        found = codecs.lookup(name).name
    except (LookupError, ValueError):
        return None
    return found if found in ALLOWED_CODECS else None


def decode_html(body: bytes, charset: str | None) -> str:
    candidates: list[str] = []
    if charset:
        candidates.append(charset)
    found = _META_CHARSET.search(body[:META_SNIFF_BYTES])
    if found:
        candidates.append(found.group(1).decode("ascii", errors="ignore"))
    for name in candidates:
        codec = _codec(name)
        if codec is not None:
            try:
                return body.decode(codec, errors="replace")
            except UnicodeError:
                continue
    return body.decode("utf-8", errors="replace")


@dataclass(slots=True)
class _Bucket:
    lines: list[str] = field(default_factory=list)
    images: int = 0
    time_attr: list[str] = field(default_factory=list)


def _ld_dates(node: object, depth: int = 0) -> list[str]:
    out: list[str] = []
    if depth > JSON_LD_MAX_DEPTH:
        return out
    if isinstance(node, list):
        for item in node:
            out.extend(_ld_dates(item, depth + 1))
    elif isinstance(node, dict):
        value = node.get("datePublished")
        if isinstance(value, str):
            out.append(value)
        out.extend(_ld_dates(node.get("@graph"), depth + 1))
    return out


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip: list[str] = []
        self.article = _Bucket()
        self.main = _Bucket()
        self.body = _Bucket()
        self.article_depth = 0
        self.main_depth = 0
        self.article_seen = False
        self.main_seen = False
        self.block_depth = 0
        self.buffer: list[str] = []
        self.in_ld = False
        self.ld_buffer: list[str] = []
        self.ld_chars = 0
        self.in_title = False
        self.title_parts: list[str] = []
        self.og_title: str | None = None
        self.first_h1: str | None = None
        self.meta_published: list[str] = []
        self.ld_published: list[str] = []
        self.itemprop_published: list[str] = []
        self.name_published: list[str] = []

    def active(self) -> list[_Bucket]:
        buckets = [self.body]
        if self.article_depth:
            buckets.append(self.article)
        if self.main_depth:
            buckets.append(self.main)
        return buckets

    def flush(self, tag: str | None = None) -> None:
        line = _SPACE.sub(" ", "".join(self.buffer)).strip()
        self.buffer = []
        if not line:
            return
        for bucket in self.active():
            bucket.lines.append(line)
        if tag == "h1" and self.first_h1 is None:
            self.first_h1 = line

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            self.read_meta(a)
            return
        if tag == "title" and not self.skip:
            self.in_title = True
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self.in_ld = True
            self.ld_buffer = []
            self.ld_chars = 0
            return
        if tag in SKIP_TAGS:
            self.skip.append(tag)
            return
        if self.skip:
            return
        if tag == "article" and (self.article_depth or not self.article_seen):
            self.article_seen = True
            self.article_depth += 1
        elif tag == "main" and (self.main_depth or not self.main_seen):
            self.main_seen = True
            self.main_depth += 1
        elif tag == "img":
            for bucket in self.active():
                bucket.images += 1
        elif tag == "time" and a.get("datetime"):
            for bucket in self.active():
                bucket.time_attr.append(a["datetime"])
        elif tag == "br" and self.block_depth:
            self.buffer.append(" ")
        elif tag in BLOCK_TAGS:
            self.flush()
            self.block_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in BLOCK_TAGS or tag in SKIP_TAGS or tag in ("article", "main"):
            return
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.in_ld:
            self.in_ld = False
            if self.ld_chars > JSON_LD_MAX_CHARS:
                return
            try:
                data = json.loads("".join(self.ld_buffer), strict=False)
            except (ValueError, RecursionError):
                return
            self.ld_published.extend(_ld_dates(data))
            return
        if tag == "title":
            self.in_title = False
        if tag in SKIP_TAGS:
            if tag in self.skip:
                while self.skip and self.skip.pop() != tag:
                    pass
            return
        if self.skip:
            return
        if tag == "article" and self.article_depth:
            self.article_depth -= 1
        elif tag == "main" and self.main_depth:
            self.main_depth -= 1
        elif tag in BLOCK_TAGS and self.block_depth:
            self.flush(tag)
            self.block_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.in_ld:
            self.ld_chars += len(data)
            if self.ld_chars <= JSON_LD_MAX_CHARS:
                self.ld_buffer.append(data)
        elif self.skip:
            return
        elif self.in_title:
            self.title_parts.append(data)
        elif self.block_depth:
            self.buffer.append(data)

    def read_meta(self, a: dict[str, str]) -> None:
        content = a.get("content", "").strip()
        if not content:
            return
        prop = a.get("property", "").lower()
        if prop == "og:title" and self.og_title is None:
            self.og_title = _SPACE.sub(" ", content)
        elif prop == "article:published_time":
            self.meta_published.append(content)
        if a.get("itemprop", "").lower() == "datepublished":
            self.itemprop_published.append(content)
        if a.get("name", "").lower() in DATE_META_NAMES:
            self.name_published.append(content)

    def close(self) -> None:
        super().close()
        self.flush()


def _first_date(candidates: list[str]) -> Published | None:
    for value in candidates:
        found = parse_iso8601(value)
        if found is not None:
            return found
    return None


def page_text(html: str) -> PageText:
    parser = _Extractor()
    parser.feed(html)
    parser.close()
    chosen = parser.body
    if parser.article_seen and parser.article.lines:
        chosen = parser.article
    elif parser.main_seen and parser.main.lines:
        chosen = parser.main
    title = parser.og_title
    if title is None:
        title = _SPACE.sub(" ", "".join(parser.title_parts)).strip() or parser.first_h1
    published = _first_date(
        parser.meta_published
        + parser.ld_published
        + parser.itemprop_published
        + parser.name_published
        + chosen.time_attr
    )
    return PageText(
        title=title,
        text=normalise_text("\n".join(chosen.lines)),
        image_count=chosen.images,
        published=published,
    )
