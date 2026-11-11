"""The one write path from a fetched document to the object store and raw_document."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

import psycopg
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from refinery_watch.db.connect import Conn
from refinery_watch.objects import RawStore
from refinery_watch.sources.published import Published

TermMatcher = Callable[[str], Sequence[str]]

DUPLICATE_CONSTRAINTS: Final = ("content_sha256", "object_key")


@dataclass(frozen=True, slots=True)
class Stored:
    raw_document_id: int
    object_key: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class Skipped:
    reason: Literal["known_hash", "known_url"]
    content_sha256: str | None


@dataclass(frozen=True, slots=True)
class NewDocument:
    source_id: str
    url: str
    original: bytes
    ext: Literal["html", "json"]
    content_type: str
    content_sha256: str
    headline: str | None
    body_chars: int
    fetched_at: datetime
    published: Published | None
    prefilter_terms: tuple[str, ...]


def _returned(row: DictRow | None) -> DictRow:
    if row is None:
        raise RuntimeError("insert returned no row")
    return row


def _require_aware(value: datetime, name: str) -> None:
    if value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def object_key(source_id: str, fetched_at: datetime, sha: str, ext: str) -> str:
    _require_aware(fetched_at, "fetched_at")
    day = fetched_at.astimezone(UTC)
    return f"raw/{source_id}/{day:%Y/%m/%d}/{sha}.{ext}"


def url_known(conn: Conn, urls: Sequence[str]) -> bool:
    row = conn.execute(
        "SELECT 1 AS found FROM raw_document WHERE url = ANY(%s) LIMIT 1", (list(urls),)
    ).fetchone()
    return row is not None


def _hash_known(conn: Conn, sha: str) -> bool:
    row = conn.execute(
        "SELECT 1 AS found FROM raw_document WHERE content_sha256 = %s", (sha,)
    ).fetchone()
    return row is not None


def store_document(
    conn: Conn, store: RawStore, job_run_id: int, doc: NewDocument
) -> Stored | Skipped:
    _require_aware(doc.fetched_at, "fetched_at")
    if doc.published is not None:
        _require_aware(doc.published.at, "published.at")
    if _hash_known(conn, doc.content_sha256):
        return Skipped("known_hash", doc.content_sha256)
    key = object_key(doc.source_id, doc.fetched_at, doc.content_sha256, doc.ext)
    store.put(key, doc.original, doc.content_type)
    terms = list(doc.prefilter_terms)
    published = doc.published
    duplicate = False
    new_id = 0
    try:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO raw_document (source_id, job_run_id, url, content_sha256,"
                " object_key, content_type, headline, body_chars, fetched_at, published_at,"
                " published_tz, prefilter_passed, prefilter_terms)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    doc.source_id,
                    job_run_id,
                    doc.url,
                    doc.content_sha256,
                    key,
                    doc.content_type,
                    doc.headline,
                    doc.body_chars,
                    doc.fetched_at,
                    published.at if published else None,
                    published.tz if published else None,
                    len(terms) > 0,
                    terms,
                ),
            ).fetchone()
            new_id = int(_returned(row)["id"])
    except psycopg.errors.UniqueViolation as exc:
        name = exc.diag.constraint_name or ""
        if not any(part in name for part in DUPLICATE_CONSTRAINTS):
            raise
        duplicate = True
    if duplicate:
        conn.commit()
        return Skipped("known_hash", doc.content_sha256)
    conn.commit()
    return Stored(new_id, key, doc.content_sha256)


def flag_thin_page(conn: Conn, raw_document_id: int, url: str, body_chars: int, images: int) -> int:
    row = conn.execute(
        "INSERT INTO review_item (raw_document_id, candidate, reason)"
        " VALUES (%s, %s, 'thin_page') RETURNING id",
        (raw_document_id, Jsonb({"url": url, "body_chars": body_chars, "images": images})),
    ).fetchone()
    new_id = int(_returned(row)["id"])
    conn.commit()
    return new_id
