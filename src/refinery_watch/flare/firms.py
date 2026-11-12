"""NASA FIRMS area queries: paths, key redaction and the VIIRS CSV parser."""

import csv
import io
import math
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final, Literal, cast

from refinery_watch.flare.footprints import Footprint
from refinery_watch.flare.geometry import BBox
from refinery_watch.sources.http import FetchError

SOURCE_ID: Final = "nasa_firms"
BASE: Final = "https://firms.modaps.eosdis.nasa.gov"
Satellite = Literal["SNPP", "NOAA20"]
Product = Literal["NRT", "SP"]
SATELLITES: Final[tuple[Satellite, ...]] = ("SNPP", "NOAA20")
LIVE_RANGE_DAYS: Final = 2
BACKFILL_RANGE_DAYS: Final = 5  # days per backfill request
MAX_RANGE_DAYS: Final = 5
MAX_BOX_DEGREES: Final = 5.0
BOX_PAD_DEGREES: Final = 0.05
EXPECTED_COLUMNS: Final = frozenset(
    {"latitude", "longitude", "acq_date", "acq_time", "frp", "confidence", "daynight"}
)
AVAILABILITY_COLUMNS: Final = frozenset({"data_id", "min_date", "max_date"})
REDACTED: Final = "REDACTED"
_CONFIDENCE: Final = frozenset({"l", "n", "h"})
_DAYNIGHT: Final = frozenset({"D", "N"})
_TIME_DIGITS: Final = 4


@dataclass(frozen=True, slots=True)
class QueryBox:
    bbox: BBox
    refinery_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Detection:
    lat: float
    lon: float
    acquired_at: datetime
    frp_mw: Decimal
    confidence: Literal["l", "n", "h"]
    daynight: Literal["D", "N"]


class FirmsResponseError(ValueError):
    """FIRMS answered with something that is not the expected CSV. The text holds no key."""


def source_name(satellite: Satellite, product: Product) -> str:
    return f"VIIRS_{satellite}_{product}"


def area_path(
    key: str, satellite: Satellite, product: Product, box: BBox, range_days: int, start: date
) -> str:
    if not 1 <= range_days <= MAX_RANGE_DAYS:
        raise ValueError(f"range_days must be 1 to {MAX_RANGE_DAYS}")
    corners = f"{box.west:.4f},{box.south:.4f},{box.east:.4f},{box.north:.4f}"
    name = source_name(satellite, product)
    return f"/api/area/csv/{key}/{name}/{corners}/{range_days}/{start:%Y-%m-%d}"


def redacted(text: str, key: str) -> str:
    """The text with every copy of the key replaced."""
    return text.replace(key, REDACTED) if key else text


def availability_path(key: str) -> str:
    return f"/api/data_availability/csv/{key}/all"


def _reader(body: bytes) -> csv.DictReader[str]:
    return csv.DictReader(io.TextIOWrapper(io.BytesIO(body), encoding="utf-8", newline=""))


def _header(reader: csv.DictReader[str], needed: frozenset[str], what: str) -> None:
    try:
        names = reader.fieldnames
    except (UnicodeDecodeError, csv.Error):
        raise FirmsResponseError(f"{what} is not UTF-8 CSV") from None
    if names is None or not needed <= set(names):
        raise FirmsResponseError(f"{what} is not the expected CSV: missing columns")


def parse_availability(body: bytes) -> dict[str, tuple[date, date]]:
    """Rows of data_id, min_date, max_date. The layout is assumed, so a bad one fails loudly."""
    reader = _reader(body)
    _header(reader, AVAILABILITY_COLUMNS, "data availability")
    out: dict[str, tuple[date, date]] = {}
    try:
        for row in reader:
            try:
                first = date.fromisoformat((row["min_date"] or "").strip())
                last = date.fromisoformat((row["max_date"] or "").strip())
            except ValueError:
                raise FirmsResponseError("data availability has a bad date") from None
            out[(row["data_id"] or "").strip()] = (first, last)
    except (UnicodeDecodeError, csv.Error):
        raise FirmsResponseError("data availability is not UTF-8 CSV") from None
    return out


def _decimal(text: str) -> Decimal:
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise FirmsResponseError("a detection row has a bad number") from None
    if not value.is_finite() or value < 0:
        raise FirmsResponseError("a detection row has a bad number")
    return value


def _float(text: str, low: float, high: float) -> float:
    try:
        value = float(text.strip())
    except ValueError:
        raise FirmsResponseError("a detection row has a bad number") from None
    if not low <= value <= high:
        raise FirmsResponseError("a detection row has a coordinate out of range")
    return value


def _acquired(day: str, clock: str) -> datetime:
    digits = clock.strip().zfill(_TIME_DIGITS)
    try:
        if len(digits) != _TIME_DIGITS or not digits.isdigit():
            raise ValueError
        return datetime.fromisoformat(f"{day.strip()}T{digits[:2]}:{digits[2:]}:00").replace(
            tzinfo=UTC
        )
    except ValueError:
        raise FirmsResponseError("a detection row has a bad date or time") from None


def _detection(row: dict[str, str | None]) -> Detection:
    if None in row or any(v is None for v in row.values()):
        raise FirmsResponseError("a detection row has the wrong number of columns")
    text = cast(dict[str, str], row)
    confidence = text["confidence"].strip().lower()
    daynight = text["daynight"].strip().upper()
    if confidence not in _CONFIDENCE or daynight not in _DAYNIGHT:
        raise FirmsResponseError("a detection row has an unknown confidence or day/night flag")
    return Detection(
        lat=_float(text["latitude"], -90.0, 90.0),
        lon=_float(text["longitude"], -180.0, 180.0),
        acquired_at=_acquired(text["acq_date"], text["acq_time"]),
        frp_mw=_decimal(text["frp"]),
        confidence=cast(Literal["l", "n", "h"], confidence),
        daynight=cast(Literal["D", "N"], daynight),
    )


def parse_area_csv(body: bytes) -> Iterator[Detection]:
    """Checks the header now, then yields one detection per row as the caller reads them."""
    reader = _reader(body)
    _header(reader, EXPECTED_COLUMNS, "FIRMS reply")
    return _rows(reader)


def _rows(reader: csv.DictReader[str]) -> Iterator[Detection]:
    try:
        for row in reader:
            yield _detection(cast(dict[str, str | None], row))
    except (UnicodeDecodeError, csv.Error):
        raise FirmsResponseError("FIRMS reply is not UTF-8 CSV") from None


def safe_note(exc: FetchError, key: str) -> str:
    """Reason, host and status only. str(exc) holds the path, and the path holds the key."""
    status = "" if exc.status is None else f" status={exc.status}"
    return redacted(f"{type(exc).__name__}: {exc.reason} host={exc.host}{status}", key)


def _requests_for(box: BBox, ids: tuple[str, ...]) -> list[QueryBox]:
    """The box clamped to the globe. One that runs past +-180 becomes two, one each side."""
    south, north = max(-90.0, box.south), min(90.0, box.north)
    if box.west < -180.0:
        return [
            QueryBox(BBox(box.west + 360.0, south, 180.0, north), ids),
            QueryBox(BBox(-180.0, south, min(box.east, 180.0), north), ids),
        ]
    if box.east > 180.0:
        return [
            QueryBox(BBox(max(box.west, -180.0), south, 180.0, north), ids),
            QueryBox(BBox(-180.0, south, box.east - 360.0, north), ids),
        ]
    return [QueryBox(BBox(box.west, south, box.east, north), ids)]


def _region_box(group: Sequence[Footprint]) -> BBox:
    box = group[0].search_box()
    for footprint in group[1:]:
        box = box.union(footprint.search_box())
    return box


def query_boxes(footprints: Sequence[Footprint]) -> list[QueryBox]:
    """One padded box per region when it is small enough, else one per plant. Sorted."""
    regions: dict[str, list[Footprint]] = {}
    for footprint in footprints:
        regions.setdefault(footprint.region, []).append(footprint)
    boxes: list[QueryBox] = []
    for region in sorted(regions):
        group = sorted(regions[region], key=lambda f: f.refinery_id)
        whole = _region_box(group)
        if whole.width() <= MAX_BOX_DEGREES and whole.height() <= MAX_BOX_DEGREES:
            ids = tuple(f.refinery_id for f in group)
            boxes.extend(_requests_for(whole.padded(BOX_PAD_DEGREES), ids))
            continue
        for footprint in group:
            padded = footprint.search_box().padded(BOX_PAD_DEGREES)
            boxes.extend(_requests_for(padded, (footprint.refinery_id,)))
    return sorted(boxes, key=lambda b: (b.refinery_ids, b.bbox.west))


MAX_GRID_CELLS: Final = 400
Cell = tuple[int, int]
Entry = tuple[Footprint, BBox]


def _grid(entries: Sequence[Entry]) -> tuple[dict[Cell, list[Entry]], list[Entry]]:
    """One-degree cells, so a detection is tested only against plants near it."""
    cells: dict[Cell, list[Entry]] = {}
    wide: list[Entry] = []
    for entry in entries:
        box = entry[1]
        rows = range(math.floor(box.south), math.floor(box.north) + 1)
        columns = range(math.floor(box.west), math.floor(box.east) + 1)
        if len(rows) * len(columns) > MAX_GRID_CELLS:
            wide.append(entry)
            continue
        for row in rows:
            for column in columns:
                wrapped = (column + 180) % 360 - 180
                cells.setdefault((row, wrapped), []).append(entry)
    return cells, wide


def assign(
    detections: Iterable[Detection], footprints: Sequence[Footprint], box: QueryBox
) -> Iterator[tuple[str, Detection]]:
    """Yields (refinery_id, detection) for each footprint a detection falls in or near."""
    wanted = set(box.refinery_ids)
    entries = [(f, f.search_box()) for f in footprints if f.refinery_id in wanted]
    cells, wide = _grid(entries)
    for detection in detections:
        cell = (math.floor(detection.lat), math.floor(detection.lon))
        for footprint, search in [*cells.get(cell, ()), *wide]:
            if search.contains_wrapped(detection.lat, detection.lon) and footprint.contains(
                detection.lat, detection.lon
            ):
                yield footprint.refinery_id, detection
