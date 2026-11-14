"""Reads the refinery and source reference tables. Never opens a CSV."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, cast

from refinery_watch.db.connect import Conn
from refinery_watch.extract.matching import RefineryRef, UnitRef


@dataclass(frozen=True, slots=True)
class SourceRef:
    id: str
    name: str
    tier: int


def load_refineries(conn: Conn) -> list[RefineryRef]:
    rows = conn.execute(
        "SELECT r.id, r.name, r.aliases, r.country, r.owner, r.nameplate_kbd, r.status,"
        " u.id AS unit_id, u.unit_code, u.unit_tag, u.capacity_kbd"
        " FROM refinery r LEFT JOIN refinery_unit u ON u.refinery_id = r.id"
        " ORDER BY r.id, u.unit_code, u.unit_tag"
    ).fetchall()
    units: dict[str, dict[str, list[UnitRef]]] = {}
    heads: dict[str, RefineryRef] = {}
    for row in rows:
        rid = str(row["id"])
        if rid not in heads:
            heads[rid] = RefineryRef(
                id=rid,
                name=row["name"],
                aliases=tuple(row["aliases"]),
                country=row["country"],
                owner=row["owner"],
                nameplate_kbd=_dec(row["nameplate_kbd"]),
                status=cast(Literal["operating", "closed_or_converted"], row["status"]),
            )
            units[rid] = {}
        if row["unit_id"] is not None:
            units[rid].setdefault(row["unit_code"], []).append(
                UnitRef(int(row["unit_id"]), row["unit_tag"], _dec(row["capacity_kbd"]))
            )
    return [
        RefineryRef(
            id=h.id,
            name=h.name,
            aliases=h.aliases,
            country=h.country,
            owner=h.owner,
            nameplate_kbd=h.nameplate_kbd,
            status=h.status,
            units={code: tuple(rows_) for code, rows_ in units[h.id].items()},
        )
        for h in heads.values()
    ]


def load_source(conn: Conn, source_id: str) -> SourceRef:
    row = conn.execute("SELECT id, name, tier FROM source WHERE id = %s", (source_id,)).fetchone()
    if row is None:
        raise LookupError(f"source {source_id} not loaded")
    return SourceRef(str(row["id"]), str(row["name"]), int(row["tier"]))


def _dec(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))
