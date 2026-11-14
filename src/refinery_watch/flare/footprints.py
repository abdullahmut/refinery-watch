"""Plant locations, footprint rows and the Overpass run that fills them."""

import csv
import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from urllib.parse import quote

from psycopg.types.json import Jsonb

from refinery_watch.db.connect import Conn
from refinery_watch.flare.geometry import (
    BUFFER_M,
    EARTH_RADIUS_M,
    BBox,
    Ring,
    haversine_m,
    ring_within_m,
)
from refinery_watch.flare.overpass import (
    ENDPOINT,
    SOURCE_ID,
    FootprintChoice,
    FootprintKind,
    OverpassResponseError,
    build_query,
    choose_footprint,
    parse_elements,
)
from refinery_watch.objects import ObjectStoreError, RawStore
from refinery_watch.sources.http import BudgetExceeded, Fetcher, FetchError, HostBlocked
from refinery_watch.sources.store import (
    NewDocument,
    RunOutcome,
    RunStatus,
    Stored,
    store_document,
)

LOCATION_COLUMNS: Final = (
    "refinery_id",
    "lat",
    "lon",
    "radius_m",
    "osm_ref",
    "source_url",
    "verified",
)
DEFAULT_RADIUS_M: Final = 2_500
MAX_LOCATIONS_BYTES: Final = 1_000_000
RADIUS_RANGE: Final = (500, 10_000)
_ID: Final = re.compile(r"R[0-9]{4}")
_REF: Final = re.compile(r"(way|relation)/[0-9]+")
_BBOX_SCALE: Final = 100_000.0


@dataclass(frozen=True, slots=True)
class Location:
    refinery_id: str
    lat: float
    lon: float
    radius_m: int
    osm_ref: str | None
    source_url: str | None
    verified: bool


@dataclass(frozen=True, slots=True)
class Footprint:
    id: int
    refinery_id: str
    kind: FootprintKind
    rings: tuple[Ring, ...]
    center_lat: float
    center_lon: float
    radius_m: int | None
    bbox: BBox
    region: str

    def search_box(self) -> BBox:
        """The bounding box widened by the buffer, for a cheap first test."""
        if self.kind == "point_radius":
            return self.bbox
        d_lat = math.degrees(BUFFER_M / EARTH_RADIUS_M)
        d_lon = d_lat / max(math.cos(math.radians(self.center_lat)), 0.01)
        return BBox(
            self.bbox.west - d_lon,
            self.bbox.south - d_lat,
            self.bbox.east + d_lon,
            self.bbox.north + d_lat,
        )

    def contains(self, lat: float, lon: float) -> bool:
        """Inside the footprint, or within one pixel (BUFFER_M) of its edge."""
        if self.kind == "point_radius":
            return haversine_m(self.center_lat, self.center_lon, lat, lon) <= (self.radius_m or 0)
        if not self.search_box().contains_wrapped(lat, lon):
            return False
        if self.kind == "osm_bounds":
            return True
        return any(ring_within_m(lat, lon, ring, BUFFER_M) for ring in self.rings)


class LocationsError(ValueError):
    """The locations file is not usable. The text names the line."""


def _fail(path: Path, line: int, message: str) -> LocationsError:
    return LocationsError(f"{path.name} line {line}: {message}")


def _float_in(path: Path, line: int, name: str, text: str, low: float, high: float) -> float:
    try:
        value = float(text)
    except ValueError:
        raise _fail(path, line, f"{name} is not a number") from None
    if not math.isfinite(value) or not low <= value <= high:
        raise _fail(path, line, f"{name} must be from {low:g} to {high:g}")
    return value


def _location(path: Path, line: int, raw: dict[str, str]) -> Location:
    refinery_id = raw["refinery_id"].strip()
    if not _ID.fullmatch(refinery_id):
        raise _fail(path, line, "refinery_id must look like R1234")
    radius_text = raw["radius_m"].strip()
    if radius_text and not radius_text.isdigit():
        raise _fail(path, line, "radius_m must be a whole number")
    low, high = RADIUS_RANGE
    radius = int(radius_text) if radius_text else DEFAULT_RADIUS_M
    if not low <= radius <= high:
        raise _fail(path, line, f"radius_m must be from {low} to {high}")
    osm_ref = raw["osm_ref"].strip() or None
    if osm_ref is not None and not _REF.fullmatch(osm_ref):
        raise _fail(path, line, "osm_ref must look like way/123 or relation/456")
    verified = raw["verified"].strip().lower()
    if verified not in ("yes", "no"):
        raise _fail(path, line, "verified must be yes or no")
    url = raw["source_url"].strip() or None
    if url is not None and not url.startswith(("http://", "https://")):
        raise _fail(path, line, "source_url must start with http:// or https://")
    return Location(
        refinery_id,
        _float_in(path, line, "lat", raw["lat"].strip(), -90, 90),
        _float_in(path, line, "lon", raw["lon"].strip(), -180, 180),
        radius,
        osm_ref,
        url,
        verified == "yes",
    )


def read_locations(path: Path) -> list[Location]:
    try:
        if path.stat().st_size > MAX_LOCATIONS_BYTES:
            raise LocationsError(f"{path.name} is over {MAX_LOCATIONS_BYTES} bytes")
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise LocationsError(f"{path.name} cannot be read as UTF-8 text") from None
    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None or set(LOCATION_COLUMNS) - set(reader.fieldnames):
        raise _fail(path, 1, "header must have " + ",".join(LOCATION_COLUMNS))
    found: dict[str, Location] = {}
    for raw in reader:
        line = reader.line_num
        if None in raw or any(value is None for value in raw.values()):
            raise _fail(path, line, "wrong number of columns")
        location = _location(path, line, raw)
        if location.refinery_id in found:
            raise _fail(path, line, f"{location.refinery_id} appears twice")
        found[location.refinery_id] = location
    return list(found.values())


def geometry_sha256(choice: FootprintChoice, lat: float, lon: float, radius_m: int) -> str:
    """Hash of everything that defines the footprint, so an unchanged one is not rewritten."""
    canonical = {
        "kind": choice.kind,
        "osm_refs": list(choice.osm_refs),
        "rings": [[list(p) for p in ring] for ring in choice.rings],
        "bbox": [choice.bbox.west, choice.bbox.south, choice.bbox.east, choice.bbox.north],
        "lat": round(lat, 5),
        "lon": round(lon, 5),
        "radius_m": radius_m if choice.kind == "point_radius" else None,
    }
    text = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _floor5(value: float) -> float:
    return math.floor(value * _BBOX_SCALE) / _BBOX_SCALE


def _ceil5(value: float) -> float:
    return math.ceil(value * _BBOX_SCALE) / _BBOX_SCALE


def write_footprint(
    conn: Conn, location: Location, choice: FootprintChoice, raw_document_id: int | None
) -> int | None:
    """Inserts a footprint that supersedes the current one. None when nothing changed."""
    sha = geometry_sha256(choice, location.lat, location.lon, location.radius_m)
    current = conn.execute(
        "SELECT id, geometry_sha256, location_verified, location_source_url"
        " FROM footprint_current_as_of(clock_timestamp()) WHERE refinery_id = %s",
        (location.refinery_id,),
    ).fetchone()
    if (
        current is not None
        and current["geometry_sha256"] == sha
        and current["location_verified"] == location.verified
        and current["location_source_url"] == location.source_url
    ):
        return None
    point = choice.kind == "point_radius"
    rings = None if point else [[list(p) for p in ring] for ring in choice.rings]
    box = choice.bbox
    row = conn.execute(
        "INSERT INTO plant_footprint (refinery_id, supersedes, kind, center_lat, center_lon,"
        " radius_m, osm_refs, rings, vertex_count, west, south, east, north, geometry_sha256,"
        " location_verified, location_source_url, raw_document_id)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " RETURNING id",
        (
            location.refinery_id,
            None if current is None else current["id"],
            choice.kind,
            location.lat,
            location.lon,
            location.radius_m if point else None,
            list(choice.osm_refs),
            None if rings is None else Jsonb(rings),
            sum(len(ring) for ring in choice.rings),
            _floor5(box.west),
            _floor5(box.south),
            _ceil5(box.east),
            _ceil5(box.north),
            sha,
            location.verified,
            location.source_url,
            raw_document_id,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("insert returned no row")
    return int(row["id"])


def _rings(raw: object) -> tuple[Ring, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(tuple((float(p[0]), float(p[1])) for p in ring) for ring in raw)


def current_footprints(conn: Conn) -> list[Footprint]:
    rows = conn.execute(
        "SELECT f.id, f.refinery_id, f.kind, f.rings, f.center_lat, f.center_lon, f.radius_m,"
        " f.west, f.south, f.east, f.north, r.region"
        " FROM footprint_current_as_of(clock_timestamp()) f"
        " JOIN refinery r ON r.id = f.refinery_id ORDER BY f.refinery_id"
    ).fetchall()
    return [
        Footprint(
            id=int(r["id"]),
            refinery_id=r["refinery_id"],
            kind=r["kind"],
            rings=_rings(r["rings"]),
            center_lat=float(r["center_lat"]),
            center_lon=float(r["center_lon"]),
            radius_m=None if r["radius_m"] is None else int(r["radius_m"]),
            bbox=BBox(float(r["west"]), float(r["south"]), float(r["east"]), float(r["north"])),
            region=r["region"],
        )
        for r in rows
    ]


def _visit_order(conn: Conn, locations: Sequence[Location]) -> list[Location]:
    """Plants with no footprint first, then the one fetched longest ago."""
    rows = conn.execute(
        "SELECT refinery_id, known_at FROM footprint_current_as_of(clock_timestamp())"
    ).fetchall()
    known: dict[str, datetime] = {r["refinery_id"]: r["known_at"] for r in rows}
    never = datetime.min.replace(tzinfo=UTC)

    def key(location: Location) -> tuple[int, datetime, str]:
        stamp = known.get(location.refinery_id)
        return (
            (0, never, location.refinery_id) if stamp is None else (1, stamp, location.refinery_id)
        )

    return sorted(locations, key=key)


def _known_ids(conn: Conn) -> set[str]:
    return {r["id"] for r in conn.execute("SELECT id FROM refinery").fetchall()}


def _query_url(query: str) -> str:
    return f"{ENDPOINT}?data={quote(query, safe='')}"


def _fetch_one(
    conn: Conn, fetcher: Fetcher, store: RawStore, run_id: int, location: Location
) -> bool:
    """True when a footprint row was written, False when unchanged. Raises on a bad reply."""
    query = build_query(location.lat, location.lon, location.osm_ref)
    reply = fetcher.get(ENDPOINT, params=[("data", query)], accept="application/json")
    shapes = parse_elements(reply.body)
    choice = choose_footprint(
        location.lat, location.lon, location.radius_m, location.osm_ref, shapes
    )
    written: list[int | None] = []

    def write_in_same_transaction(document_id: int) -> None:
        written.append(write_footprint(conn, location, choice, document_id))

    content_sha = hashlib.sha256(query.encode("utf-8") + b"\n" + reply.body).hexdigest()
    result = store_document(
        conn,
        store,
        run_id,
        NewDocument(
            source_id=SOURCE_ID,
            url=_query_url(query),
            original=reply.body,
            ext="json",
            content_type="application/json",
            content_sha256=content_sha,
            headline=f"OpenStreetMap outline for {location.refinery_id}",
            body_chars=len(reply.body),
            fetched_at=reply.fetched_at,
            published=None,
            prefilter_terms=(),
        ),
        in_same_tx=write_in_same_transaction,
    )
    if isinstance(result, Stored):
        return written[0] is not None
    return _write_for_known_reply(conn, location, choice, content_sha)


def _write_for_known_reply(
    conn: Conn, location: Location, choice: FootprintChoice, content_sha: str
) -> bool:
    """The reply is already stored, but the locations file may have changed since."""
    row = conn.execute(
        "SELECT id FROM raw_document WHERE content_sha256 = %s", (content_sha,)
    ).fetchone()
    document_id = None if row is None else int(row["id"])
    written = write_footprint(conn, location, choice, document_id) is not None
    conn.commit()
    return written


def run_footprints(
    conn: Conn,
    fetcher: Fetcher,
    store: RawStore,
    run_id: int,
    locations: Sequence[Location],
    max_requests: int,
    deadline: float,
) -> RunOutcome:
    fetcher.set_deadline(deadline)
    known_refineries = _known_ids(conn)
    conn.rollback()
    ordered = _visit_order(conn, [loc for loc in locations if loc.refinery_id in known_refineries])
    conn.rollback()
    notes = [
        f"{loc.refinery_id} is not a known refinery" for loc in locations if loc not in ordered
    ]
    requests = 0
    written = 0
    failures = len(notes)
    for location in ordered[:max_requests]:
        requests += 1
        try:
            written += 1 if _fetch_one(conn, fetcher, store, run_id, location) else 0
        except (HostBlocked, BudgetExceeded) as exc:
            notes.append(f"{location.refinery_id}: stopped, {exc.reason}")
            failures += 1
            break
        except FetchError as exc:
            notes.append(f"{location.refinery_id}: fetch {exc.reason}")
            failures += 1
        except (OverpassResponseError, ObjectStoreError) as exc:
            notes.append(f"{location.refinery_id}: {exc}")
            failures += 1
    left = len(ordered) - min(len(ordered), max_requests)
    if left:
        notes.append(f"{left} plants left for the next run")
    status: RunStatus = "failed" if failures else "ok"
    return RunOutcome(status, requests, written, "; ".join(notes) or None)
