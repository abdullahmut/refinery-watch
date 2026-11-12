import math

import pytest

from refinery_watch.flare.footprints import Footprint
from refinery_watch.flare.geometry import (
    EARTH_RADIUS_M,
    BBox,
    Ring,
    haversine_m,
    radius_bbox,
    ring_bbox,
)

LAT = 29.7
LON = -95.0
HALF = 0.01
SQUARE: Ring = (
    (LON - HALF, LAT - HALF),
    (LON + HALF, LAT - HALF),
    (LON + HALF, LAT + HALF),
    (LON - HALF, LAT + HALF),
    (LON - HALF, LAT - HALF),
)


def polygon() -> Footprint:
    return Footprint(
        1, "R9001", "osm_polygon", (SQUARE,), LAT, LON, None, ring_bbox(SQUARE), "US Gulf"
    )


def circle(radius_m: int = 2500) -> Footprint:
    return Footprint(
        2,
        "R9001",
        "point_radius",
        (),
        LAT,
        LON,
        radius_m,
        radius_bbox(LAT, LON, radius_m),
        "US Gulf",
    )


def bounds() -> Footprint:
    box = BBox(LON - HALF, LAT - HALF, LON + HALF, LAT + HALF)
    return Footprint(3, "R9001", "osm_bounds", (), LAT, LON, None, box, "US Gulf")


def east_of_edge(metres: float) -> float:
    return LON + HALF + metres / (EARTH_RADIUS_M * math.cos(math.radians(LAT)) * math.pi / 180)


def test_polygon_counts_370_m_outside_but_not_380_m() -> None:
    assert polygon().contains(LAT, east_of_edge(370))
    assert not polygon().contains(LAT, east_of_edge(380))


def test_polygon_counts_inside_and_far_outside() -> None:
    assert polygon().contains(LAT, LON)
    assert not polygon().contains(LAT + 1, LON)


def test_polygon_corner_distance_is_diagonal() -> None:
    d = 300 / (EARTH_RADIUS_M * math.pi / 180)
    assert not polygon().contains(LAT + HALF + d, east_of_edge(300))


def test_circle_edge_is_the_radius() -> None:
    d_lat = 1499 / (EARTH_RADIUS_M * math.pi / 180)
    assert haversine_m(LAT, LON, LAT + d_lat, LON) < 1500
    assert circle(1500).contains(LAT + d_lat, LON)
    d_lat = 1501 / (EARTH_RADIUS_M * math.pi / 180)
    assert not circle(1500).contains(LAT + d_lat, LON)


def test_circle_gets_no_buffer() -> None:
    assert circle(500).contains(LAT, LON)
    d_lat = 600 / (EARTH_RADIUS_M * math.pi / 180)
    assert not circle(500).contains(LAT + d_lat, LON)


def test_bounds_use_the_box_with_the_buffer() -> None:
    assert bounds().contains(LAT, LON)
    assert bounds().contains(LAT, east_of_edge(300))
    assert not bounds().contains(LAT, east_of_edge(500))


@pytest.mark.parametrize("footprint", [polygon(), bounds(), circle()], ids=lambda f: f.kind)
def test_search_box_holds_the_centre(footprint: Footprint) -> None:
    assert footprint.search_box().contains(LAT, LON)
