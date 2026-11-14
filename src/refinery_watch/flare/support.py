"""Reads of flare support for outage rows. Thin wrappers over the SQL functions."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from refinery_watch.db.connect import Conn
from refinery_watch.db.events import require_aware


@dataclass(frozen=True, slots=True)
class EffectiveConfidence:
    outage_event_id: int
    stored: str
    effective: str
    flare_day_id: int | None  # the flagged day that lifted the row, else None


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: Literal["outage_near", "outage_open", "regulator_near"]
    ref_id: int
    item_date: date
    event_type: str | None
    unit_label: str | None
    confidence: str | None
    whole_plant: bool = False


def confidence_as_of(conn: Conn, as_of: datetime) -> list[EffectiveConfidence]:
    """Stored and effective confidence of every current outage row as of `as_of`."""
    require_aware(as_of, "as_of")
    rows = conn.execute(
        "SELECT outage_event_id, stored, effective, flare_day_id"
        " FROM outage_confidence_as_of(%s) ORDER BY outage_event_id",
        (as_of,),
    ).fetchall()
    return [
        EffectiveConfidence(
            int(r["outage_event_id"]), r["stored"], r["effective"], r["flare_day_id"]
        )
        for r in rows
    ]


def evidence_as_of(conn: Conn, refinery_id: str, day: date, as_of: datetime) -> list[Evidence]:
    """News, open-outage and regulator items for a plant around `day`, known by `as_of`."""
    require_aware(as_of, "as_of")
    rows = conn.execute(
        "SELECT kind, ref_id, item_date, event_type, unit_label, confidence, whole_plant"
        " FROM flare_evidence_as_of(%s, %s, %s)",
        (refinery_id, day, as_of),
    ).fetchall()
    return [
        Evidence(
            r["kind"],
            int(r["ref_id"]),
            r["item_date"],
            r["event_type"],
            r["unit_label"],
            r["confidence"],
            bool(r["whole_plant"]),
        )
        for r in rows
    ]
