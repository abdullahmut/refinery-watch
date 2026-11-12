"""Synthetic flare history with a fixed seed. Nothing here is real data."""

import math
import random
from dataclasses import dataclass
from datetime import date, timedelta

from tests.db.flare_helpers import add_day, add_footprint

from refinery_watch.db.connect import Conn

SEED = 20261112
FIRST_DAY = date(2025, 1, 1)
PLANTS = ("R9001", "R9002", "R9003")

# Planted noise: how many quiet plant-days carry each score. The rest sit well under 3.
PLANTED = ((3.1, 40), (4.2, 20), (5.1, 6), (7.3, 2))


@dataclass(frozen=True, slots=True)
class SynthDay:
    refinery_id: str
    day: date
    z: float
    n_detections: int


def quiet_history(days: int = 400, plants: tuple[str, ...] = PLANTS) -> list[SynthDay]:
    """Scored plant-days: planted high scores spread over the plants, ordinary noise elsewhere."""
    rng = random.Random(SEED)  # noqa: S311
    cells = [(p, FIRST_DAY + timedelta(days=i)) for p in plants for i in range(days)]
    rng.shuffle(cells)
    scores: dict[tuple[str, date], float] = {}
    cursor = 0
    for z, count in PLANTED:
        for cell in cells[cursor : cursor + count]:
            scores[cell] = z
        cursor += count
    out = []
    for plant, day in sorted(cells, key=lambda c: (c[0], c[1])):
        z = scores.get((plant, day), round(rng.uniform(-2.0, 2.9), 2))
        out.append(SynthDay(plant, day, z, 3 if z >= 3.0 else 0))
    return out


def load(conn: Conn, history: list[SynthDay]) -> dict[str, int]:
    """Inserts one footprint per plant and one flare_day row per entry. Returns footprint ids."""
    footprints = {p: add_footprint(conn, p) for p in sorted({h.refinery_id for h in history})}
    for h in history:
        add_day(
            conn,
            footprints[h.refinery_id],
            h.day,
            refinery_id=h.refinery_id,
            n_detections=h.n_detections,
            frp_sum_mw=h.n_detections * 5,
            x=math.log(1 + h.n_detections * 5),
            baseline_median=0.0,
            baseline_mad=0.5,
            z=h.z,
        )
    return footprints


def flagged_count(history: list[SynthDay], z_min: float, min_detections: int = 1) -> int:
    return sum(1 for h in history if h.z >= z_min and h.n_detections >= min_detections)
