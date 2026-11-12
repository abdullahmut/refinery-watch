"""FIRMS fetches: store each reply, then write its detections and coverage, then score."""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from refinery_watch.db.connect import Conn
from refinery_watch.flare.firms import (
    BASE,
    LIVE_RANGE_DAYS,
    SATELLITES,
    SOURCE_ID,
    Detection,
    FirmsResponseError,
    Product,
    QueryBox,
    Satellite,
    area_path,
    assign,
    parse_area_csv,
    query_boxes,
    redacted,
    safe_note,
    source_name,
)
from refinery_watch.flare.footprints import Footprint, current_footprints
from refinery_watch.flare.score import write_days
from refinery_watch.objects import ObjectStoreError, RawStore
from refinery_watch.sources.http import BudgetExceeded, Fetcher, FetchError, HostBlocked
from refinery_watch.sources.store import NewDocument, RunOutcome, RunStatus, Stored, store_document

NO_FOOTPRINTS: Final = "no footprints; run the footprints command first"
REDACTED_BYTES: Final = b"REDACTED"


@dataclass(frozen=True, slots=True)
class FetchPlan:
    satellite: Satellite
    product: Product
    box: QueryBox
    start: date
    range_days: int

    def days(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(self.range_days)]


def live_plans(boxes: Sequence[QueryBox], today: date) -> list[FetchPlan]:
    """NRT for both satellites, yesterday and today."""
    start = today - timedelta(days=1)
    return [
        FetchPlan(sat, "NRT", box, start, LIVE_RANGE_DAYS) for box in boxes for sat in SATELLITES
    ]


def _content_prefix(plan: FetchPlan) -> bytes:
    box = plan.box.bbox
    corners = f"{box.west:.4f},{box.south:.4f},{box.east:.4f},{box.north:.4f}"
    name = source_name(plan.satellite, plan.product)
    return f"{name}|{corners}|{plan.start:%Y-%m-%d}|{plan.range_days}\n".encode()


def _clean_body(body: bytes, key: str) -> bytes:
    cleaned = body.replace(key.encode(), REDACTED_BYTES) if key else body
    if key and key.encode() in cleaned:
        raise RuntimeError("key redaction failed")
    return cleaned


def _write_rows(
    conn: Conn,
    plan: FetchPlan,
    hits: Sequence[tuple[str, Detection]],
    footprints: Sequence[Footprint],
    document_id: int,
) -> int:
    footprint_of = {f.refinery_id: f.id for f in footprints}
    written = 0
    for refinery_id, d in hits:
        written += conn.execute(
            "INSERT INTO flare_detection (refinery_id, footprint_id, satellite, product,"
            " acquired_at, latitude, longitude, frp_mw, confidence, daynight, raw_document_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT DO NOTHING",
            (
                refinery_id,
                footprint_of[refinery_id],
                plan.satellite,
                plan.product,
                d.acquired_at,
                d.lat,
                d.lon,
                d.frp_mw,
                d.confidence,
                d.daynight,
                document_id,
            ),
        ).rowcount
    conn.execute(
        "INSERT INTO flare_coverage (refinery_id, day, satellite, product, raw_document_id)"
        " SELECT r, %s::date + g, %s, %s, %s"
        " FROM unnest(%s::text[]) AS r, generate_series(0, %s) AS g"
        " WHERE NOT EXISTS (SELECT 1 FROM flare_coverage c WHERE c.refinery_id = r"
        " AND c.day = %s::date + g AND c.satellite = %s AND c.product = %s)",
        (
            plan.start,
            plan.satellite,
            plan.product,
            document_id,
            list(plan.box.refinery_ids),
            plan.range_days - 1,
            plan.start,
            plan.satellite,
            plan.product,
        ),
    )
    return written


def fetch_one(
    conn: Conn,
    fetcher: Fetcher,
    store: RawStore,
    run_id: int,
    key: str,
    plan: FetchPlan,
    footprints: Sequence[Footprint],
) -> int:
    """One area request. Returns the detections written; 0 for a reply already stored."""
    path = area_path(key, plan.satellite, plan.product, plan.box.bbox, plan.range_days, plan.start)
    reply = fetcher.get(BASE + path, accept="text/csv")
    body = _clean_body(reply.body, key)
    hits = list(assign(parse_area_csv(body), footprints, plan.box))
    written: list[int] = []

    def write_in_same_transaction(document_id: int) -> None:
        written.append(_write_rows(conn, plan, hits, footprints, document_id))

    name = source_name(plan.satellite, plan.product)
    result = store_document(
        conn,
        store,
        run_id,
        NewDocument(
            source_id=SOURCE_ID,
            url=redacted(BASE + path, key),
            original=body,
            ext="csv",
            content_type="text/csv",
            content_sha256=hashlib.sha256(_content_prefix(plan) + body).hexdigest(),
            headline=f"FIRMS {name} from {plan.start:%Y-%m-%d}",
            body_chars=len(body),
            fetched_at=reply.fetched_at,
            published=None,
            prefilter_terms=(),
        ),
        in_same_tx=write_in_same_transaction,
    )
    return written[0] if isinstance(result, Stored) else 0


@dataclass(slots=True)
class Tally:
    requests: int = 0
    detections: int = 0
    failures: int = 0
    notes: list[str] | None = None
    touched: dict[tuple[str, ...], set[date]] | None = None

    def note(self, text: str) -> None:
        if self.notes is None:
            self.notes = []
        self.notes.append(text)

    def touch(self, plan: FetchPlan) -> None:
        if self.touched is None:
            self.touched = {}
        self.touched.setdefault(plan.box.refinery_ids, set()).update(plan.days())


def run_plans(
    conn: Conn,
    fetcher: Fetcher,
    store: RawStore,
    run_id: int,
    key: str,
    plans: Sequence[FetchPlan],
    footprints: Sequence[Footprint],
) -> Tally:
    """Fetches each plan. A failed plan is noted and the rest go on; a blocked host stops all."""
    tally = Tally()
    for plan in plans:
        tally.requests += 1
        try:
            tally.detections += fetch_one(conn, fetcher, store, run_id, key, plan, footprints)
        except (HostBlocked, BudgetExceeded) as exc:
            tally.failures += 1
            tally.note(safe_note(exc, key))
            break
        except FetchError as exc:
            tally.failures += 1
            tally.note(safe_note(exc, key))
            continue
        except (FirmsResponseError, ObjectStoreError) as exc:
            tally.failures += 1
            tally.note(redacted(f"{type(exc).__name__}: {exc}", key))
            continue
        tally.touch(plan)
    return tally


def score_touched(conn: Conn, tally: Tally, backfilled: bool, run_id: int) -> int:
    """Writes flare_day rows for every plant and day a successful fetch covered."""
    written = 0
    for ids, days in sorted((tally.touched or {}).items()):
        written += write_days(conn, ids, sorted(days), backfilled, run_id)
    return written


def _outcome(tally: Tally) -> RunOutcome:
    status: RunStatus = "failed" if tally.failures else "ok"
    note = "; ".join(tally.notes) if tally.notes else None
    return RunOutcome(status, tally.requests, tally.detections, note)


def run_fetch(
    conn: Conn,
    fetcher: Fetcher,
    store: RawStore,
    run_id: int,
    key: str,
    today: date,
    deadline: float,
) -> RunOutcome:
    fetcher.set_deadline(deadline)
    footprints = current_footprints(conn)
    conn.rollback()
    if not footprints:
        return RunOutcome("failed", 0, 0, NO_FOOTPRINTS)
    plans = live_plans(query_boxes(footprints), today)
    tally = run_plans(conn, fetcher, store, run_id, key, plans, footprints)
    score_touched(conn, tally, False, run_id)
    return _outcome(tally)
