"""Overpass query and the choice of a plant footprint from the reply."""

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from refinery_watch.flare.geometry import (
    EARTH_RADIUS_M,
    MAX_VERTICES,
    BBox,
    Ring,
    close_ring,
    point_in_ring,
    ring_bbox,
)

SOURCE_ID: Final = "osm_overpass"
ENDPOINT: Final = "https://overpass-api.de/api/interpreter"
SEARCH_RADIUS_M: Final = 5_000
OSM_REF: Final = re.compile(r"(way|relation)/([0-9]+)")

FootprintKind = Literal["osm_polygon", "osm_bounds", "point_radius"]


@dataclass(frozen=True, slots=True)
class OsmShape:
    ref: str  # "way/123" or "relation/456"
    rings: tuple[Ring, ...]  # closed outer rings; empty if none could be closed
    bounds: BBox | None


@dataclass(frozen=True, slots=True)
class FootprintChoice:
    kind: FootprintKind
    osm_refs: tuple[str, ...]
    rings: tuple[Ring, ...]  # empty for point_radius
    bbox: BBox


class OverpassResponseError(ValueError):
    """The reply is not a usable Overpass result."""


def build_query(lat: float, lon: float, osm_ref: str | None) -> str:
    if osm_ref is not None:
        found = OSM_REF.fullmatch(osm_ref)
        if found is None:
            raise ValueError("osm_ref must look like way/123 or relation/456")
        return f"[out:json][timeout:60];{found.group(1)}({found.group(2)});out geom;"
    around = f"(around:{SEARCH_RADIUS_M},{lat:.5f},{lon:.5f})"
    return (
        "[out:json][timeout:60];"
        f'(way["industrial"="refinery"]{around};relation["industrial"="refinery"]{around};);'
        "out geom;"
    )


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _points(geometry: object) -> list[tuple[float, float]]:
    if not isinstance(geometry, list):
        raise OverpassResponseError("geometry is not a list")
    out: list[tuple[float, float]] = []
    for node in geometry:
        lat = _as_float(node.get("lat")) if isinstance(node, dict) else None
        lon = _as_float(node.get("lon")) if isinstance(node, dict) else None
        if lat is None or lon is None:
            raise OverpassResponseError("geometry node has no coordinates")
        out.append((lon, lat))
    return out


def _bounds(raw: object) -> BBox | None:
    if not isinstance(raw, dict):
        return None
    west, south, east, north = (
        _as_float(raw.get(key)) for key in ("minlon", "minlat", "maxlon", "maxlat")
    )
    if west is None or south is None or east is None or north is None:
        return None
    return BBox(west, south, east, north) if west < east and south < north else None


def _points_bbox(points: Sequence[tuple[float, float]]) -> BBox | None:
    if not points:
        return None
    box = ring_bbox(tuple(points))
    return box if box.west < box.east and box.south < box.north else None


def _way_shape(element: Mapping[str, object]) -> OsmShape:
    points = _points(element.get("geometry", []))
    ring = close_ring(points)
    bounds = _bounds(element.get("bounds")) or _points_bbox(points)
    return OsmShape(f"way/{element['id']}", (ring,) if ring else (), bounds)


def _relation_shape(element: Mapping[str, object]) -> OsmShape:
    members = element.get("members", [])
    if not isinstance(members, list):
        raise OverpassResponseError("members is not a list")
    rings: list[Ring] = []
    every_point: list[tuple[float, float]] = []
    for member in members:
        if not isinstance(member, dict) or member.get("role") != "outer":
            continue
        points = _points(member.get("geometry", []))
        every_point.extend(points)
        ring = close_ring(points)
        if ring:
            rings.append(ring)
    bounds = _bounds(element.get("bounds")) or _points_bbox(every_point)
    return OsmShape(f"relation/{element['id']}", tuple(rings), bounds)


def parse_elements(body: bytes) -> list[OsmShape]:
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        raise OverpassResponseError("Overpass reply is not JSON") from None
    if not isinstance(data, dict):
        raise OverpassResponseError("Overpass reply is not a JSON object")
    if "remark" in data:
        raise OverpassResponseError("Overpass reported a runtime error")
    elements = data.get("elements")
    if not isinstance(elements, list):
        raise OverpassResponseError("Overpass reply has no elements list")
    shapes: list[OsmShape] = []
    for element in elements:
        if not isinstance(element, dict) or not isinstance(element.get("id"), int):
            continue
        if element.get("type") == "way":
            shapes.append(_way_shape(element))
        elif element.get("type") == "relation":
            shapes.append(_relation_shape(element))
    return shapes


def radius_bbox(lat: float, lon: float, radius_m: int) -> BBox:
    d_lat = math.degrees(radius_m / EARTH_RADIUS_M)
    d_lon = d_lat / max(math.cos(math.radians(lat)), 0.01)
    return BBox(lon - d_lon, lat - d_lat, lon + d_lon, lat + d_lat)


def _vertices(rings: Sequence[Ring]) -> int:
    return sum(len(ring) for ring in rings)


def _union(boxes: Sequence[BBox]) -> BBox:
    box = boxes[0]
    for other in boxes[1:]:
        box = box.union(other)
    return box


def _polygon(shapes: Sequence[OsmShape]) -> FootprintChoice:
    rings = tuple(ring for shape in shapes for ring in shape.rings)
    refs = tuple(shape.ref for shape in shapes)
    return FootprintChoice("osm_polygon", refs, rings, _union([ring_bbox(r) for r in rings]))


def _fallback(lat: float, lon: float, radius_m: int) -> FootprintChoice:
    return FootprintChoice("point_radius", (), (), radius_bbox(lat, lon, radius_m))


def _named(shapes: Sequence[OsmShape], osm_ref: str) -> FootprintChoice:
    shape = next((s for s in shapes if s.ref == osm_ref), None)
    if shape is None:
        raise OverpassResponseError(f"{osm_ref} is not in the Overpass reply")
    if shape.rings:
        return _polygon([shape])
    if shape.bounds is not None:
        return FootprintChoice("osm_bounds", (shape.ref,), (), shape.bounds)
    raise OverpassResponseError(f"{osm_ref} has no outline")


def _by_point(lat: float, lon: float, shapes: Sequence[OsmShape]) -> FootprintChoice | None:
    containing = [s for s in shapes if any(point_in_ring(lat, lon, r) for r in s.rings)]
    if containing:
        return _polygon(containing)
    open_relations = [
        s
        for s in shapes
        if s.ref.startswith("relation/")
        and not s.rings
        and s.bounds is not None
        and s.bounds.contains(lat, lon)
    ]
    if open_relations:
        boxes = [s.bounds for s in open_relations if s.bounds is not None]
        return FootprintChoice(
            "osm_bounds", tuple(s.ref for s in open_relations), (), _union(boxes)
        )
    return None


def choose_footprint(
    lat: float, lon: float, radius_m: int, osm_ref: str | None, shapes: Sequence[OsmShape]
) -> FootprintChoice:
    choice = _named(shapes, osm_ref) if osm_ref is not None else _by_point(lat, lon, shapes)
    if choice is None or _vertices(choice.rings) > MAX_VERTICES:
        return _fallback(lat, lon, radius_m)
    return choice
