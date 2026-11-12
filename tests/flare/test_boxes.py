import math
from datetime import UTC, datetime
from decimal import Decimal

from refinery_watch.flare.firms import Detection, QueryBox, assign, query_boxes
from refinery_watch.flare.footprints import Footprint
from refinery_watch.flare.geometry import BBox, Ring, radius_bbox

WHEN = datetime(2026, 11, 11, 7, 42, tzinfo=UTC)


def square(lat: float, lon: float, half: float = 0.01) -> Ring:
    return (
        (lon - half, lat - half),
        (lon + half, lat - half),
        (lon + half, lat + half),
        (lon - half, lat + half),
        (lon - half, lat - half),
    )


def plant(n: int, region: str, lat: float, lon: float, half: float = 0.01) -> Footprint:
    ring = square(lat, lon, half)
    box = BBox(lon - half, lat - half, lon + half, lat + half)
    return Footprint(n, f"R{9000 + n}", "osm_polygon", (ring,), lat, lon, None, box, region)


def circle(n: int, region: str, lat: float, lon: float, radius_m: int = 1000) -> Footprint:
    box = radius_bbox(lat, lon, radius_m)
    return Footprint(n, f"R{9000 + n}", "point_radius", (), lat, lon, radius_m, box, region)


def hit(lat: float, lon: float) -> Detection:
    return Detection(lat, lon, WHEN, Decimal("5.0"), "n", "N")


def test_three_plants_in_one_region_give_one_padded_box() -> None:
    plants = [
        plant(3, "US Gulf", 29.7, -95.0),
        plant(1, "US Gulf", 30.0, -93.0),
        plant(2, "US Gulf", 29.0, -91.0),
    ]
    boxes = query_boxes(plants)
    assert len(boxes) == 1
    assert boxes[0].refinery_ids == ("R9001", "R9002", "R9003")
    box = boxes[0].bbox
    assert box.west < -95.01 - 0.04
    assert box.east > -91.01 + 0.04
    assert box.south < 28.99 - 0.04
    assert box.north > 30.01 + 0.04


def test_a_region_spanning_forty_degrees_gives_one_box_per_plant() -> None:
    plants = [
        plant(1, "NE Asia", 35.0, 120.0),
        plant(2, "NE Asia", 36.0, 160.0),
        plant(3, "NE Asia", 34.0, 130.0),
    ]
    boxes = query_boxes(plants)
    assert [b.refinery_ids for b in boxes] == [("R9001",), ("R9002",), ("R9003",)]
    assert all(b.bbox.width() < 0.2 for b in boxes)


def test_regions_are_kept_apart() -> None:
    plants = [plant(1, "Med", 36.0, 14.0), plant(2, "US Gulf", 29.0, -95.0)]
    assert [b.refinery_ids for b in query_boxes(plants)] == [("R9001",), ("R9002",)]


def test_output_order_does_not_depend_on_input_order() -> None:
    plants = [plant(n, "US Gulf", 29.0 + n * 0.2, -95.0) for n in range(1, 6)]
    plants += [plant(n, "Med", 36.0 + n * 3, 14.0) for n in range(6, 9)]
    forward = query_boxes(plants)
    assert forward == query_boxes(list(reversed(plants)))
    assert forward == sorted(forward, key=lambda b: b.refinery_ids)


def test_boxes_are_clamped_to_the_globe() -> None:
    boxes = query_boxes([plant(1, "Other", 89.99, 100.0, 0.005)])
    assert boxes[0].bbox.north == 90.0
    assert len(boxes) == 1


def test_no_footprints_no_boxes() -> None:
    assert query_boxes([]) == []


def test_five_degrees_is_the_limit_for_a_shared_box() -> None:
    a = plant(1, "US Gulf", 29.0, -95.0, 0.0001)
    near = plant(2, "US Gulf", 29.0, -90.1, 0.0001)
    far = plant(3, "US Gulf", 29.0, -89.9, 0.0001)
    assert len(query_boxes([a, near])) == 1
    assert len(query_boxes([a, far])) == 2


def test_a_detection_200_m_outside_two_adjacent_plants_goes_to_both() -> None:
    gap = 400 / (111_195 * math.cos(math.radians(29.7)))
    left = plant(1, "US Gulf", 29.7, -95.0)
    right = plant(2, "US Gulf", 29.7, -95.0 + 0.02 + gap)
    middle = hit(29.7, -95.0 + 0.01 + gap / 2)
    box = QueryBox(BBox(-96, 29, -94, 30), ("R9001", "R9002"))
    found = list(assign([middle], [left, right], box))
    assert sorted(r for r, _ in found) == ["R9001", "R9002"]
    assert all(d is middle for _, d in found)


def test_a_detection_in_the_box_but_outside_every_footprint_goes_nowhere() -> None:
    left = plant(1, "US Gulf", 29.7, -95.0)
    box = QueryBox(BBox(-96, 29, -94, 30), ("R9001",))
    assert list(assign([hit(29.9, -95.0), hit(29.7, -94.5)], [left], box)) == []


def test_a_detection_inside_goes_to_its_plant_only() -> None:
    left = plant(1, "US Gulf", 29.7, -95.0)
    other = plant(2, "US Gulf", 29.7, -94.5)
    box = QueryBox(BBox(-96, 29, -94, 30), ("R9001", "R9002"))
    found = list(assign([hit(29.7, -95.0), hit(29.705, -94.5)], [left, other], box))
    assert [r for r, _ in found] == ["R9001", "R9002"]


def test_plants_outside_the_query_box_are_not_considered() -> None:
    left = plant(1, "US Gulf", 29.7, -95.0)
    other = plant(2, "US Gulf", 29.7, -94.5)
    box = QueryBox(BBox(-96, 29, -94, 30), ("R9001",))
    assert list(assign([hit(29.7, -94.5)], [left, other], box)) == []


def test_a_circle_plant_counts_a_detection_inside_its_radius() -> None:
    near = circle(1, "US Gulf", 29.7, -95.0, 1000)
    box = QueryBox(BBox(-96, 29, -94, 30), ("R9001",))
    assert len(list(assign([hit(29.704, -95.0), hit(29.72, -95.0)], [near], box))) == 1


def test_a_very_wide_footprint_is_still_searched() -> None:
    wide = plant(1, "Other", 0.0, 0.0, 10.0)
    box = QueryBox(BBox(-20, -20, 20, 20), ("R9001",))
    assert [r for r, _ in assign([hit(1.0, 1.0), hit(15.0, 15.0)], [wide], box)] == ["R9001"]


def test_assign_is_lazy() -> None:
    left = plant(1, "US Gulf", 29.7, -95.0)
    box = QueryBox(BBox(-96, 29, -94, 30), ("R9001",))

    def stream() -> object:
        yield hit(29.7, -95.0)
        raise AssertionError("read too far")

    iterator = assign(stream(), [left], box)  # type: ignore[arg-type]
    assert next(iterator)[0] == "R9001"


def test_a_tall_region_gives_one_box_per_plant() -> None:
    boxes = query_boxes([circle(1, "Other", 20.0, 50.0), circle(2, "Other", 30.0, 50.5)])
    assert [b.refinery_ids for b in boxes] == [("R9001",), ("R9002",)]


def test_a_plant_on_the_antimeridian_is_requested_on_both_sides() -> None:
    plant = circle(1, "Other", -16.5, 179.99)
    boxes = query_boxes([plant])
    assert len(boxes) == 2
    assert {b.refinery_ids for b in boxes} == {("R9001",)}
    assert all(-180.0 <= b.bbox.west < b.bbox.east <= 180.0 for b in boxes)
    assert any(b.bbox.contains(-16.5, -179.995) for b in boxes)
    assert any(b.bbox.contains(-16.5, 179.95) for b in boxes)


def test_a_plant_just_west_of_the_antimeridian_is_requested_on_both_sides() -> None:
    boxes = query_boxes([circle(1, "Other", 10.0, -179.99)])
    assert len(boxes) == 2
    assert any(b.bbox.contains(10.0, 179.995) for b in boxes)
    assert any(b.bbox.contains(10.0, -179.95) for b in boxes)


def test_a_detection_across_the_antimeridian_is_assigned() -> None:
    plant = circle(1, "Other", -16.5, 179.99, 2500)
    box = QueryBox(BBox(-180.0, -17.0, -179.0, -16.0), ("R9001",))
    found = list(assign([hit(-16.5, -179.995), hit(-16.5, 179.5)], [plant], box))
    assert [(r, d.lon) for r, d in found] == [("R9001", -179.995)]
    assert plant.contains(-16.5, -179.995)
