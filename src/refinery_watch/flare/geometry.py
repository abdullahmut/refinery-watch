"""Plain-Python geometry for plant footprints. Rings are (lon, lat) pairs."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

Ring = tuple[tuple[float, float], ...]

EARTH_RADIUS_M: Final = 6_371_008.8
BUFFER_M: Final = 375.0
MAX_VERTICES: Final = 5_000
MIN_RING_POINTS: Final = 4
EDGE_TOLERANCE_M: Final = 0.01
_M_PER_DEGREE: Final = EARTH_RADIUS_M * math.pi / 180.0


@dataclass(frozen=True, slots=True)
class BBox:
    west: float
    south: float
    east: float
    north: float

    def contains(self, lat: float, lon: float) -> bool:
        return self.south <= lat <= self.north and self.west <= lon <= self.east

    def padded(self, degrees: float) -> "BBox":
        return BBox(
            self.west - degrees, self.south - degrees, self.east + degrees, self.north + degrees
        )

    def union(self, other: "BBox") -> "BBox":
        return BBox(
            min(self.west, other.west),
            min(self.south, other.south),
            max(self.east, other.east),
            max(self.north, other.north),
        )

    def width(self) -> float:
        return self.east - self.west

    def height(self) -> float:
        return self.north - self.south


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _local_xy(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """Metres east and north of (lat0, lon0), flat-earth."""
    x = (lon - lon0) * math.cos(math.radians(lat0)) * _M_PER_DEGREE
    y = (lat - lat0) * _M_PER_DEGREE
    return x, y


def _segment_distance(ax: float, ay: float, bx: float, by: float) -> float:
    """Distance from the origin to the segment a-b."""
    dx = bx - ax
    dy = by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0:
        return math.hypot(ax, ay)
    t = max(0.0, min(1.0, -(ax * dx + ay * dy) / length_sq))
    return math.hypot(ax + t * dx, ay + t * dy)


def distance_to_ring_m(lat: float, lon: float, ring: Ring) -> float:
    """Metres from the point to the nearest edge, inside or outside. Equirectangular."""
    best = math.inf
    previous = _local_xy(lat, lon, ring[0][1], ring[0][0])
    for vertex_lon, vertex_lat in ring[1:]:
        current = _local_xy(lat, lon, vertex_lat, vertex_lon)
        best = min(best, _segment_distance(*previous, *current))
        previous = current
    return best


def point_in_ring(lat: float, lon: float, ring: Ring) -> bool:
    """Ray casting. A point on an edge or vertex counts as inside."""
    if distance_to_ring_m(lat, lon, ring) <= EDGE_TOLERANCE_M:
        return True
    inside = False
    x1, y1 = ring[0]
    for x2, y2 in ring[1:]:
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
        x1, y1 = x2, y2
    return inside


def ring_bbox(ring: Ring) -> BBox:
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return BBox(min(lons), min(lats), max(lons), max(lats))


def close_ring(points: Sequence[tuple[float, float]]) -> Ring | None:
    """The points as a ring when they have at least 4 and already end where they start."""
    if len(points) < MIN_RING_POINTS or points[0] != points[-1]:
        return None
    return tuple(points)
