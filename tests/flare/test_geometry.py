import math

import pytest

from refinery_watch.flare.geometry import (
    BUFFER_M,
    EARTH_RADIUS_M,
    BBox,
    Ring,
    as_closed_ring,
    distance_to_ring_m,
    haversine_m,
    point_in_ring,
    ring_bbox,
    ring_within_m,
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
# A U shape: the notch is the open space between x = -0.5 and 0.5 above y = 0.
CONCAVE: Ring = (
    (-1.0, 0.0),
    (1.0, 0.0),
    (1.0, 2.0),
    (0.5, 2.0),
    (0.5, 1.0),
    (-0.5, 1.0),
    (-0.5, 2.0),
    (-1.0, 2.0),
    (-1.0, 0.0),
)


def metres_east(lat: float, metres: float) -> float:
    return metres / (EARTH_RADIUS_M * math.cos(math.radians(lat)) * math.pi / 180)


def test_inside_and_outside_a_square() -> None:
    assert point_in_ring(LAT, LON, SQUARE)
    assert not point_in_ring(LAT, LON + 0.02, SQUARE)
    assert not point_in_ring(LAT + 0.02, LON, SQUARE)


def test_a_vertex_and_an_edge_count_as_inside() -> None:
    assert point_in_ring(LAT - HALF, LON - HALF, SQUARE)
    assert point_in_ring(LAT, LON + HALF, SQUARE)


def test_the_notch_of_a_concave_ring_is_outside() -> None:
    assert not point_in_ring(1.5, 0.0, CONCAVE)
    assert point_in_ring(0.5, 0.0, CONCAVE)
    assert point_in_ring(1.5, 0.75, CONCAVE)
    assert point_in_ring(1.5, -0.75, CONCAVE)
    assert not point_in_ring(2.5, 0.0, CONCAVE)


def test_distance_from_300_m_outside_an_edge() -> None:
    lon = LON + HALF + metres_east(LAT, 300)
    assert distance_to_ring_m(LAT, lon, SQUARE) == pytest.approx(300, abs=2)


def test_distance_to_a_corner_is_the_diagonal() -> None:
    d = metres_east(LAT, 300)
    got = distance_to_ring_m(LAT + HALF + 300 / 111_195, LON + HALF + d, SQUARE)
    assert got == pytest.approx(math.hypot(300, 300), abs=2)


def test_distance_inside_is_to_the_nearest_edge() -> None:
    assert distance_to_ring_m(LAT, LON, SQUARE) == pytest.approx(
        HALF * math.pi / 180 * EARTH_RADIUS_M * math.cos(math.radians(LAT)), abs=2
    )


def test_distance_to_a_degenerate_edge() -> None:
    ring: Ring = ((0.0, 0.0), (0.0, 0.0), (1.0, 0.0), (0.0, 0.0))
    assert distance_to_ring_m(0.0, 0.5, ring) == pytest.approx(0.0, abs=1e-6)


def test_haversine_known_distances() -> None:
    assert haversine_m(LAT, LON, LAT, LON) == 0.0
    one_degree = haversine_m(0.0, 0.0, 1.0, 0.0)
    assert one_degree == pytest.approx(111_195, abs=5)
    assert haversine_m(10.0, 20.0, 12.0, 25.0) == pytest.approx(haversine_m(12.0, 25.0, 10.0, 20.0))


def test_buffer_is_one_pixel() -> None:
    assert BUFFER_M == 375.0


def test_ring_bbox() -> None:
    box = ring_bbox(SQUARE)
    assert (box.west, box.south, box.east, box.north) == (
        LON - HALF,
        LAT - HALF,
        LON + HALF,
        LAT + HALF,
    )


def test_bbox_contains_includes_the_edges() -> None:
    box = BBox(-1.0, 2.0, 3.0, 4.0)
    assert box.contains(2.0, -1.0)
    assert box.contains(4.0, 3.0)
    assert not box.contains(4.1, 0.0)
    assert not box.contains(3.0, 3.1)


def test_bbox_padded_union_width_height() -> None:
    box = BBox(0.0, 0.0, 2.0, 1.0)
    assert box.padded(0.5) == BBox(-0.5, -0.5, 2.5, 1.5)
    assert box.union(BBox(-1.0, 0.5, 1.0, 3.0)) == BBox(-1.0, 0.0, 2.0, 3.0)
    assert (box.width(), box.height()) == (2.0, 1.0)


def test_as_closed_ring_needs_four_closed_points() -> None:
    assert as_closed_ring(list(SQUARE)) == SQUARE
    assert as_closed_ring(list(SQUARE)[:3]) is None
    assert as_closed_ring([]) is None
    open_ring = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    assert as_closed_ring(open_ring) is None
    triangle = [(0.0, 0.0), (1.0, 0.0), (0.0, 0.0)]
    assert as_closed_ring(triangle) is None


def test_ring_within_m_inside_near_and_far() -> None:
    assert ring_within_m(LAT, LON, SQUARE, 0.0)
    near = LON + HALF + metres_east(LAT, 300)
    assert ring_within_m(LAT, near, SQUARE, 375.0)
    assert not ring_within_m(LAT, near, SQUARE, 250.0)
    assert not ring_within_m(LAT + 1, LON, SQUARE, 375.0)
