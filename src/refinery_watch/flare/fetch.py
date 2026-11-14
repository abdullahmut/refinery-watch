"""FIRMS fetches: store each reply, then write its detections and coverage, then score."""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Final

from refinery_watch.db.connect import Conn
from refinery_watch.flare.firms import (
    BACKFILL_RANGE_DAYS,
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
    availability_path,
    parse_area_csv,
    parse_availability,
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
AVAILABILITY_REQUESTS: Final = 1  # the data availability call counts against max_requests
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
    plants = ",".join(sorted(plan.box.refinery_ids))
    return f"{name}|{corners}|{plan.start:%Y-%m-%d}|{plan.range_days}|{plants}\n".encode()


def _clean_body(body: bytes, key: str) -> bytes:
    return body.replace(key.encode(), REDACTED_BYTES) if key else body


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
    reply = fetcher.get(BASE + path, accept="text/csv", same_host=True)
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
    notes: list[str] = field(default_factory=list)
    touched: dict[tuple[str, ...], set[date]] = field(default_factory=dict)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def touch(self, plan: FetchPlan) -> None:
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
    for ids, days in sorted(tally.touched.items()):
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


def _chunks(
    satellite: Satellite, availability: Mapping[str, tuple[date, date]], first: date, last: date
) -> list[tuple[Product, date, int]]:
    """SP up to the SP last date, NRT after. A chunk never crosses that date."""
    entry = availability.get(source_name(satellite, "SP"))
    sp_last = entry[1] if entry else None
    out: list[tuple[Product, date, int]] = []
    day = first
    while day <= last:
        product: Product = "SP" if sp_last is not None and day <= sp_last else "NRT"
        stop = min(last, day + timedelta(days=BACKFILL_RANGE_DAYS - 1))
        if product == "SP" and sp_last is not None:
            stop = min(stop, sp_last)
        out.append((product, day, (stop - day).days + 1))
        day = stop + timedelta(days=1)
    return out


def _covered(conn: Conn, plan: FetchPlan) -> bool:
    row = conn.execute(
        "SELECT count(*) AS n FROM (SELECT DISTINCT refinery_id, day FROM flare_coverage"
        " WHERE refinery_id = ANY(%s) AND satellite = %s AND product = %s"
        " AND day BETWEEN %s AND %s) t",
        (
            list(plan.box.refinery_ids),
            plan.satellite,
            plan.product,
            plan.start,
            plan.start + timedelta(days=plan.range_days - 1),
        ),
    ).fetchone()
    return row is not None and int(row["n"]) == len(plan.box.refinery_ids) * plan.range_days


def backfill_plans(
    conn: Conn,
    boxes: Sequence[QueryBox],
    availability: Mapping[str, tuple[date, date]],
    first: date,
    last: date,
) -> list[FetchPlan]:
    """Plans oldest first. A chunk is dropped when coverage already holds all its plant-days."""
    plans = [
        FetchPlan(satellite, product, box, start, days)
        for satellite in SATELLITES
        for product, start, days in _chunks(satellite, availability, first, last)
        for box in boxes
    ]
    order = {sat: i for i, sat in enumerate(SATELLITES)}
    plans.sort(key=lambda p: (p.start, p.box.refinery_ids, order[p.satellite]))
    return [p for p in plans if not _covered(conn, p)]


def run_backfill(
    conn: Conn,
    fetcher: Fetcher,
    store: RawStore,
    run_id: int,
    key: str,
    first: date,
    last: date,
    max_requests: int,
    deadline: float,
) -> RunOutcome:
    if first > last:
        return RunOutcome("failed", 0, 0, "the first day is after the last day")
    fetcher.set_deadline(deadline)
    footprints = current_footprints(conn)
    conn.rollback()
    if not footprints:
        return RunOutcome("failed", 0, 0, NO_FOOTPRINTS)
    try:
        reply = fetcher.get(BASE + availability_path(key), accept="text/csv", same_host=True)
        availability = parse_availability(_clean_body(reply.body, key))
    except FetchError as exc:
        return RunOutcome("failed", 1, 0, safe_note(exc, key))
    except FirmsResponseError as exc:
        return RunOutcome("failed", 1, 0, redacted(f"{type(exc).__name__}: {exc}", key))
    plans = backfill_plans(conn, query_boxes(footprints), availability, first, last)
    conn.rollback()
    budget = max(0, max_requests - AVAILABILITY_REQUESTS)
    tally = run_plans(conn, fetcher, store, run_id, key, plans[:budget], footprints)
    tally.requests += AVAILABILITY_REQUESTS
    score_touched(conn, tally, True, run_id)
    left = len(plans) - min(len(plans), budget)
    if left:
        tally.note(f"{left} requests left; run again")
    return _outcome(tally)
