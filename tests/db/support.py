"""Row builders for the edge and open-issue tests."""

from decimal import Decimal
from typing import Any

from tests.db.helpers import add_document, day, ts, unit_id

from refinery_watch.db.connect import Conn
from refinery_watch.db.events import NewEventRow, SourceLink

R = "R9001"


def link(conn: Conn, n: int) -> SourceLink:
    return SourceLink(add_document(conn, n), "company_statement", "quoted text")


def outage(conn: Conn, d: int, known: int, **over: Any) -> NewEventRow:
    fields: dict[str, Any] = {
        "event_type": "unplanned",
        "refinery_id": R,
        "refinery_unit_id": unit_id(conn),
        "whole_plant": False,
        "start_date": day(d),
        "confidence": "high",
        "capacity_basis": "stated",
        "match_score": 100,
        "capacity_affected_kbd": Decimal(200),
        "known_at": ts(known),
    }
    fields.update(over)
    return NewEventRow(**fields)


def restart(conn: Conn, d: int, known: int, **over: Any) -> NewEventRow:
    fields: dict[str, Any] = {
        "event_type": "restart",
        "capacity_basis": "not_stated",
        "capacity_affected_kbd": None,
    }
    fields.update(over)
    return outage(conn, d, known, **fields)
