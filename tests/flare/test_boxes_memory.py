"""The parse-and-assign path holds one reply and a few objects, not the whole file twice."""

import math
import random
import tracemalloc

from refinery_watch.flare.firms import QueryBox, assign, parse_area_csv
from refinery_watch.flare.footprints import Footprint
from refinery_watch.flare.geometry import BBox, Ring, ring_bbox

PLANTS = 200
VERTICES = 2_000
ROWS = 40_000
LIMIT_BYTES = 64 * 1024 * 1024
HEADER = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,"
    "confidence,version,bright_ti5,frp,daynight\n"
)


def circle_ring(lat: float, lon: float, radius_deg: float) -> Ring:
    points = [
        (
            lon + radius_deg * math.cos(2 * math.pi * i / (VERTICES - 1)),
            lat + radius_deg * math.sin(2 * math.pi * i / (VERTICES - 1)),
        )
        for i in range(VERTICES - 1)
    ]
    return (*points, points[0])


def footprints() -> list[Footprint]:
    out: list[Footprint] = []
    for n in range(PLANTS):
        lat, lon = 20.0 + (n // 20) * 0.5, -100.0 + (n % 20) * 0.5
        ring = circle_ring(lat, lon, 0.01)
        out.append(
            Footprint(
                n + 1,
                f"R{1000 + n}",
                "osm_polygon",
                (ring,),
                lat,
                lon,
                None,
                ring_bbox(ring),
                "US Gulf",
            )
        )
    return out


def csv_body(plants: list[Footprint]) -> bytes:
    rng = random.Random(7)  # noqa: S311
    lines = [HEADER]
    for i in range(ROWS):
        if i % 100 == 0:
            plant = plants[rng.randrange(PLANTS)]
            lat = plant.center_lat + rng.uniform(-0.012, 0.012)
            lon = plant.center_lon + rng.uniform(-0.012, 0.012)
        else:
            lat, lon = rng.uniform(15.0, 19.0), rng.uniform(-110.0, -90.0)
        clock = f"{rng.randrange(24):02d}{rng.randrange(60):02d}"
        lines.append(
            f"{lat:.5f},{lon:.5f},{rng.uniform(300, 360):.15f},{rng.uniform(0.3, 0.5):.12f},"
            f"{rng.uniform(0.3, 0.5):.12f},2026-11-11,{clock},N20,VIIRS,n,"
            f"2.0NRT,{rng.uniform(280, 300):.15f},{rng.uniform(1, 40):.2f},N\n"
        )
    return "".join(lines).encode()


def test_parse_and_assign_peak_stays_under_64_mb() -> None:
    plants = footprints()
    body = csv_body(plants)
    assert 4_500_000 < len(body) < 6_000_000
    box = QueryBox(BBox(-115, 10, -85, 35), tuple(p.refinery_id for p in plants))
    tracemalloc.start()
    try:
        assigned = sum(1 for _ in assign(parse_area_csv(body), plants, box))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert assigned > 100
    assert peak < LIMIT_BYTES
