"""Picks a z threshold from quiet history. The counting is SQL; this module only picks a row."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final, Literal

from psycopg.types.json import Jsonb

from refinery_watch.db.connect import Conn
from refinery_watch.db.events import require_aware

GRID: Final = tuple(Decimal("3.00") + Decimal("0.25") * i for i in range(29))  # 3.00 to 10.00
QUIET_WINDOW_DAYS: Final = 7
MIN_PLANT_DAYS: Final = 1_000
DEFAULT_TARGET: Final[Mapping[str, Decimal]] = {
    "signal": Decimal("1.0"),
    "alert": Decimal("0.2"),
}

# A quiet plant-day is a scored day with no outage row start and no regulator item within a week.
QUIET_SQL: Final = """
WITH outages AS MATERIALIZED (
  SELECT refinery_id, start_date FROM outage_current_as_of(%(as_of)s)
), regulator AS MATERIALIZED (
  SELECT refinery_id, (coalesce(started_at, known_at) AT TIME ZONE 'UTC')::date AS item_date
  FROM tceq_event WHERE refinery_id IS NOT NULL AND known_at <= %(as_of)s
), quiet AS (
  SELECT d.z, d.n_detections
  FROM flare_day_current_as_of(%(as_of)s) d
  WHERE d.z IS NOT NULL AND d.day BETWEEN %(first)s AND %(last)s
    AND NOT EXISTS (SELECT 1 FROM outages o WHERE o.refinery_id = d.refinery_id
                    AND o.start_date BETWEEN d.day - %(window)s AND d.day + %(window)s)
    AND NOT EXISTS (SELECT 1 FROM regulator t WHERE t.refinery_id = d.refinery_id
                    AND t.item_date BETWEEN d.day - %(window)s AND d.day + %(window)s)
)
SELECT count(*) AS quiet_plant_days,
       count(*) FILTER (WHERE z >= %(z)s AND n_detections >= %(min_detections)s) AS flagged,
       round((count(*) FILTER (WHERE z >= %(z)s AND n_detections >= %(min_detections)s))
             * 30.4375 / nullif(count(*), 0), 4) AS per_plant_month
FROM quiet
"""


@dataclass(frozen=True, slots=True)
class CurvePoint:
    z_min: Decimal
    flagged_quiet_days: int
    quiet_plant_days: int
    false_alarms_per_plant_month: Decimal


class NotEnoughHistory(RuntimeError):  # noqa: N818
    """Too few quiet scored plant-days to set a threshold."""


def _min_detections(conn: Conn, kind: str, as_of: datetime) -> int:
    row = conn.execute(
        "SELECT min_detections FROM flare_threshold_as_of(%s, %s) WHERE id IS NOT NULL",
        (kind, as_of),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"no {kind} threshold is in force")
    return int(row["min_detections"])


def curve(
    conn: Conn, kind: Literal["signal", "alert"], first: date, last: date, as_of: datetime
) -> list[CurvePoint]:
    """One SQL query per grid value. Raises NotEnoughHistory under MIN_PLANT_DAYS quiet days."""
    require_aware(as_of, "as_of")
    min_detections = _min_detections(conn, kind, as_of)
    points: list[CurvePoint] = []
    for z in GRID:
        row = conn.execute(
            QUIET_SQL,
            {
                "as_of": as_of,
                "first": first,
                "last": last,
                "window": QUIET_WINDOW_DAYS,
                "z": z,
                "min_detections": min_detections,
            },
        ).fetchone()
        if row is None:
            raise RuntimeError("query returned no row")
        if int(row["quiet_plant_days"]) < MIN_PLANT_DAYS:
            raise NotEnoughHistory(
                f"{row['quiet_plant_days']} quiet scored plant-days; {MIN_PLANT_DAYS} are needed"
            )
        points.append(
            CurvePoint(
                z,
                int(row["flagged"]),
                int(row["quiet_plant_days"]),
                row["per_plant_month"],
            )
        )
    return points


def choose(points: Sequence[CurvePoint], target: Decimal) -> CurvePoint | None:
    """The lowest z_min whose false alarm rate is at or under the target."""
    fits = [p for p in points if p.false_alarms_per_plant_month <= target]
    return min(fits, key=lambda p: p.z_min) if fits else None


def write_threshold(
    conn: Conn, kind: str, point: CurvePoint, min_detections: int, params: Mapping[str, str]
) -> int:
    """Inserts one calibrated row. Does not commit."""
    row = conn.execute(
        "INSERT INTO flare_threshold (kind, z_min, min_detections, method, params)"
        " VALUES (%s, %s, %s, 'calibrated', %s) RETURNING id",
        (kind, point.z_min, min_detections, Jsonb(dict(params))),
    ).fetchone()
    if row is None:
        raise RuntimeError("insert returned no row")
    return int(row["id"])
