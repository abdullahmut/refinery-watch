import json
from collections.abc import Callable
from pathlib import Path

import pytest

from refinery_watch.flare.geometry import MAX_VERTICES, radius_bbox
from refinery_watch.flare.overpass import (
    FootprintChoice,
    OsmShape,
    OverpassResponseError,
    build_query,
    choose_footprint,
    parse_elements,
)

HANDMADE = Path(__file__).resolve().parents[1] / "fixtures" / "sources" / "handmade"
LAT = 29.74500
LON = -95.01200
RADIUS = 2500
Pick = Callable[[str], Path]


def shapes_of(name: str) -> list[OsmShape]:
    return parse_elements((HANDMADE / name).read_bytes())


def choose(name: str, osm_ref: str | None = None) -> FootprintChoice:
    return choose_footprint(LAT, LON, RADIUS, osm_ref, shapes_of(name))


def test_query_without_a_ref_is_exact() -> None:
    expected = (
        '[out:json][timeout:60];(way["industrial"="refinery"](around:5000,29.74512,-95.01234);'
        'relation["industrial"="refinery"](around:5000,29.74512,-95.01234););out geom;'
    )
    assert build_query(29.74512, -95.01234, None) == expected


def test_query_rounds_to_five_decimals() -> None:
    assert "(around:5000,29.74513,-95.01235)" in build_query(29.745126, -95.012346, None)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("way/123", "[out:json][timeout:60];way(123);out geom;"),
        ("relation/456", "[out:json][timeout:60];relation(456);out geom;"),
    ],
)
def test_query_with_a_ref(ref: str, expected: str) -> None:
    assert build_query(0.0, 0.0, ref) == expected


@pytest.mark.parametrize("bad", ["node/1", "way/", "way/1;out", "123", "way/1\n", ""])
def test_query_refuses_a_bad_ref(bad: str) -> None:
    with pytest.raises(ValueError, match="osm_ref"):
        build_query(0.0, 0.0, bad)


def test_one_closed_way_containing_the_point() -> None:
    choice = choose("overpass_one_way_handmade.json")
    assert choice.kind == "osm_polygon"
    assert choice.osm_refs == ("way/9000001",)
    assert len(choice.rings) == 1
    assert (choice.bbox.west, choice.bbox.north) == (-95.022, 29.755)


def test_two_ways_only_the_containing_one_is_used() -> None:
    choice = choose("overpass_two_ways_handmade.json")
    assert choice.kind == "osm_polygon"
    assert choice.osm_refs == ("way/9000001",)


def test_a_relation_with_an_unclosed_outer_gives_bounds() -> None:
    choice = choose("overpass_relation_open_handmade.json")
    assert choice.kind == "osm_bounds"
    assert choice.osm_refs == ("relation/9000003",)
    assert choice.rings == ()
    assert (choice.bbox.south, choice.bbox.east) == (29.73, -95.0)


def test_a_relation_with_a_closed_outer_gives_a_polygon_and_drops_inner() -> None:
    shapes = shapes_of("overpass_relation_closed_handmade.json")
    assert len(shapes[0].rings) == 1
    assert choose("overpass_relation_closed_handmade.json").kind == "osm_polygon"


def test_an_empty_reply_gives_the_point_and_radius() -> None:
    choice = choose("overpass_empty_handmade.json")
    assert choice.kind == "point_radius"
    assert choice.rings == ()
    assert choice.osm_refs == ()
    assert choice.bbox == radius_bbox(LAT, LON, RADIUS)


def test_the_radius_box_covers_the_radius() -> None:
    box = radius_bbox(LAT, LON, RADIUS)
    assert box.contains(LAT + 0.0224, LON)
    assert not box.contains(LAT + 0.0226, LON)
    assert box.height() == pytest.approx(2 * 0.02248, abs=1e-4)


def test_a_point_outside_every_polygon_falls_back_to_the_circle() -> None:
    choice = choose_footprint(30.5, -96.0, 1000, None, shapes_of("overpass_one_way_handmade.json"))
    assert choice.kind == "point_radius"


def test_a_remark_reply_raises() -> None:
    with pytest.raises(OverpassResponseError, match="runtime error"):
        parse_elements((HANDMADE / "overpass_remark_handmade.json").read_bytes())


@pytest.mark.parametrize(
    "body", [b"not json", b"[]", b'{"version": 1}', b'{"elements": 5}', b"\xff"]
)
def test_unusable_replies_raise(body: bytes) -> None:
    with pytest.raises(OverpassResponseError):
        parse_elements(body)


def test_a_named_ref_picks_that_shape() -> None:
    choice = choose("overpass_two_ways_handmade.json", "way/9000002")
    assert choice.kind == "osm_polygon"
    assert choice.osm_refs == ("way/9000002",)
    assert choice.bbox.south == 29.80


def test_a_named_relation_without_a_ring_gives_bounds() -> None:
    choice = choose("overpass_relation_open_handmade.json", "relation/9000003")
    assert choice.kind == "osm_bounds"


def test_a_ref_missing_from_the_reply_raises() -> None:
    with pytest.raises(OverpassResponseError, match="way/1 is not in"):
        choose("overpass_one_way_handmade.json", "way/1")


def test_a_shape_with_no_outline_raises_when_named() -> None:
    shapes = [OsmShape("way/5", (), None)]
    with pytest.raises(OverpassResponseError, match="no outline"):
        choose_footprint(LAT, LON, RADIUS, "way/5", shapes)


def way_with_vertices(count: int) -> bytes:
    """A box round the point whose bottom edge carries extra collinear vertices."""
    extra = count - 5
    ring = [{"lat": 29.0, "lon": -96.0 + 2.0 * i / (extra + 1)} for i in range(extra + 1)]
    ring += [
        {"lat": 29.0, "lon": -94.0},
        {"lat": 30.5, "lon": -94.0},
        {"lat": 30.5, "lon": -96.0},
        {"lat": 29.0, "lon": -96.0},
    ]
    body = {"elements": [{"type": "way", "id": 7, "geometry": ring}]}
    return json.dumps(body).encode()


def test_a_ring_over_the_vertex_limit_falls_back() -> None:
    shapes = parse_elements(way_with_vertices(MAX_VERTICES + 1))
    assert len(shapes[0].rings[0]) == MAX_VERTICES + 1
    choice = choose_footprint(LAT, LON, RADIUS, "way/7", shapes)
    assert choice.kind == "point_radius"


def test_a_ring_at_the_vertex_limit_is_kept() -> None:
    shapes = parse_elements(way_with_vertices(MAX_VERTICES))
    choice = choose_footprint(LAT, LON, RADIUS, "way/7", shapes)
    assert choice.kind == "osm_polygon"


def test_elements_without_ids_or_of_other_types_are_skipped() -> None:
    body = {
        "elements": [
            {"type": "node", "id": 1, "lat": 1, "lon": 2},
            {"type": "way", "geometry": []},
            "text",
            {"type": "way", "id": 3, "geometry": []},
        ]
    }
    shapes = parse_elements(json.dumps(body).encode())
    assert [s.ref for s in shapes] == ["way/3"]
    assert shapes[0].rings == ()
    assert shapes[0].bounds is None


@pytest.mark.parametrize(
    "geometry",
    [
        [{"lat": 1}],
        ["x"],
        [{"lat": True, "lon": 2}],
        [{"lat": float("nan"), "lon": 2}],
        "x",
    ],
)
def test_bad_geometry_raises(geometry: object) -> None:
    body = json.dumps(
        {"elements": [{"type": "way", "id": 1, "geometry": geometry}]}, allow_nan=True
    )
    with pytest.raises(OverpassResponseError):
        parse_elements(body.encode())


def test_bad_members_raise() -> None:
    body = {"elements": [{"type": "relation", "id": 1, "members": "x"}]}
    with pytest.raises(OverpassResponseError):
        parse_elements(json.dumps(body).encode())


def test_bounds_come_from_the_points_when_absent() -> None:
    geometry = [{"lat": 1, "lon": 2}, {"lat": 1, "lon": 3}, {"lat": 2, "lon": 3}]
    body = {"elements": [{"type": "way", "id": 1, "geometry": geometry}]}
    shape = parse_elements(json.dumps(body).encode())[0]
    assert shape.rings == ()
    assert shape.bounds is not None
    assert (shape.bounds.west, shape.bounds.north) == (2.0, 2.0)


def test_real_overpass_reply_parses(real_fixture: Pick) -> None:
    path = real_fixture("overpass/overpass_refinery_*.json")
    assert len(parse_elements(path.read_bytes())) >= 1
