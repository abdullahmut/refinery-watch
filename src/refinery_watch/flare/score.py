"""Scores flare days. The median, MAD and z are all computed in SQL by flare_day_write."""

from collections.abc import Sequence
from datetime import date

from refinery_watch.db.connect import Conn


def write_days(
    conn: Conn, refinery_ids: Sequence[str], days: Sequence[date], backfilled: bool, run_id: int
) -> int:
    """Writes a flare_day row for each (day, plant) pair with new figures. Commits once."""
    written = 0
    for day in sorted(set(days)):
        for refinery_id in sorted(set(refinery_ids)):
            row = conn.execute(
                "SELECT flare_day_write(%s, %s, %s, %s) AS id",
                (refinery_id, day, backfilled, run_id),
            ).fetchone()
            if row is not None and row["id"] is not None:
                written += 1
    conn.commit()
    return written
