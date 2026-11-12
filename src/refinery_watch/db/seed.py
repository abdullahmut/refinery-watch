"""Loads the source and refinery lists. Operator command: python -m refinery_watch.db.seed."""

import argparse
import csv
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import psycopg

from refinery_watch.config import ConfigError, app_database_url, refineries_csv_path
from refinery_watch.db.connect import Conn, connect

Row = dict[str, Any]
Parser = Callable[[dict[str, str]], Row]

SOURCE_COLUMNS = (
    "source_id",
    "name",
    "tier",
    "region_focus",
    "access_method",
    "feed_url",
    "article_url_pattern",
    "robots_status",
    "fetch_interval_min",
    "notes",
    "verified",
)
SOURCE_VALUE_COLUMNS = SOURCE_COLUMNS[1:]

REFINERY_COLUMNS = (
    "refinery_id",
    "name",
    "aliases",
    "country",
    "region",
    "owner",
    "nameplate_kbd",
    "major_units",
    "status",
    "source_url",
    "verified",
    "verified_on",
    "notes",
)
REFINERY_VALUE_COLUMNS = tuple(
    c for c in REFINERY_COLUMNS if c not in ("refinery_id", "major_units")
)
UNIT_CODES = frozenset(
    {"CDU", "VDU", "FCC", "HCU", "COK", "REF", "HDT_D", "HDT_G", "ALK", "ISOM", "SRU", "H2", "RHU"}
)


@dataclass(frozen=True, slots=True)
class LoadResult:
    inserted: int
    updated: int
    unchanged: int
    missing_from_file: tuple[str, ...]


class SeedError(ValueError):
    """A bad row. The message names file, line number and column."""


def fail(path: Path, line: int, column: str, message: str) -> SeedError:
    return SeedError(f"{path.name}:{line}: column {column}: {message}")


def read_rows(path: Path, columns: Sequence[str]) -> list[tuple[int, dict[str, str]]]:
    """Returns (line number, row) pairs. The header must hold exactly the given columns."""
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None or tuple(header) != tuple(columns):
            raise fail(path, 1, "header", f"expected {','.join(columns)}")
        rows: list[tuple[int, dict[str, str]]] = []
        while True:
            line = reader.line_num + 1
            values = next(reader, None)
            if values is None:
                return rows
            if len(values) != len(columns):
                raise fail(path, line, "row", f"expected {len(columns)} fields, got {len(values)}")
            rows.append((line, dict(zip(columns, values, strict=True))))


def parse_yes_no(path: Path, line: int, column: str, value: str) -> bool:
    if value not in ("yes", "no"):
        raise fail(path, line, column, f"must be yes or no, got {value!r}")
    return value == "yes"


def parse_int(path: Path, line: int, column: str, value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise fail(path, line, column, f"must be a whole number, got {value!r}") from None


def parse_source(path: Path, line: int, raw: dict[str, str]) -> Row:
    row: Row = dict(raw)
    row["id"] = row.pop("source_id")
    row["tier"] = parse_int(path, line, "tier", raw["tier"])
    row["fetch_interval_min"] = parse_int(
        path, line, "fetch_interval_min", raw["fetch_interval_min"]
    )
    row["verified"] = parse_yes_no(path, line, "verified", raw["verified"])
    return row


def database_error(path: Path, line: int, exc: psycopg.Error) -> SeedError:
    column = exc.diag.column_name or exc.diag.constraint_name or "unknown"
    return fail(path, line, column, exc.diag.message_primary or str(exc))


def upsert_source(conn: Conn, row: Row) -> str:
    """Returns 'inserted', 'updated' or 'unchanged'."""
    found = conn.execute("SELECT * FROM source WHERE id = %(id)s", row).fetchone()
    if found is None:
        conn.execute(
            "INSERT INTO source (id, name, tier, region_focus, access_method, feed_url,"
            " article_url_pattern, robots_status, fetch_interval_min, notes, verified)"
            " VALUES (%(id)s, %(name)s, %(tier)s, %(region_focus)s, %(access_method)s,"
            " %(feed_url)s, %(article_url_pattern)s, %(robots_status)s,"
            " %(fetch_interval_min)s, %(notes)s, %(verified)s)",
            row,
        )
        return "inserted"
    if all(found[c] == row[c] for c in SOURCE_VALUE_COLUMNS):
        return "unchanged"
    conn.execute(
        "UPDATE source SET name = %(name)s, tier = %(tier)s, region_focus = %(region_focus)s,"
        " access_method = %(access_method)s, feed_url = %(feed_url)s,"
        " article_url_pattern = %(article_url_pattern)s, robots_status = %(robots_status)s,"
        " fetch_interval_min = %(fetch_interval_min)s, notes = %(notes)s,"
        " verified = %(verified)s, loaded_at = now() WHERE id = %(id)s",
        row,
    )
    return "updated"


def missing_ids(conn: Conn, table: str, seen: Sequence[str]) -> tuple[str, ...]:
    rows = conn.execute(
        f"SELECT id FROM {table} WHERE NOT (id = ANY(%s)) ORDER BY id",  # noqa: S608
        (list(seen),),
    ).fetchall()
    return tuple(r["id"] for r in rows)


def tally(outcomes: Sequence[str], missing: tuple[str, ...]) -> LoadResult:
    return LoadResult(
        inserted=outcomes.count("inserted"),
        updated=outcomes.count("updated"),
        unchanged=outcomes.count("unchanged"),
        missing_from_file=missing,
    )


def load_sources(conn: Conn, path: Path) -> LoadResult:
    rows = read_rows(path, SOURCE_COLUMNS)
    outcomes: list[str] = []
    with conn.transaction():
        for line, raw in rows:
            row = parse_source(path, line, raw)
            try:
                with conn.transaction():
                    outcomes.append(upsert_source(conn, row))
            except psycopg.Error as exc:
                raise database_error(path, line, exc) from exc
        missing = missing_ids(conn, "source", [r["source_id"] for _, r in rows])
    return tally(outcomes, missing)


def split_list(value: str) -> list[str]:
    return [token.strip() for token in value.split(";") if token.strip()]


def blank_to_none(value: str) -> str | None:
    return value or None


def parse_decimal(path: Path, line: int, column: str, value: str) -> Decimal | None:
    if not value:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise fail(path, line, column, f"must be a number, got {value!r}") from None
    if not number.is_finite():
        raise fail(path, line, column, f"must be a finite number, got {value!r}")
    return number


def parse_date(path: Path, line: int, column: str, value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise fail(path, line, column, f"must be YYYY-MM-DD, got {value!r}") from None


def parse_refinery(path: Path, line: int, raw: dict[str, str]) -> Row:
    return {
        "id": raw["refinery_id"],
        "name": raw["name"],
        "aliases": split_list(raw["aliases"]),
        "country": raw["country"],
        "region": raw["region"],
        "owner": blank_to_none(raw["owner"]),
        "nameplate_kbd": parse_decimal(path, line, "nameplate_kbd", raw["nameplate_kbd"]),
        "status": raw["status"],
        "source_url": blank_to_none(raw["source_url"]),
        "verified": parse_yes_no(path, line, "verified", raw["verified"]),
        "verified_on": parse_date(path, line, "verified_on", raw["verified_on"]),
        "notes": blank_to_none(raw["notes"]),
        "units": split_list(raw["major_units"]),
    }


def bad_unit_errors(path: Path, rows: Sequence[tuple[int, dict[str, str]]]) -> list[str]:
    """Lists every major_units token that is not a unit code, with its line and refinery."""
    problems: list[str] = []
    for line, raw in rows:
        bad = [t for t in split_list(raw["major_units"]) if t not in UNIT_CODES]
        if bad:
            tokens = ", ".join(repr(t) for t in bad)
            problems.append(
                f"{path.name}:{line}: column major_units: {raw['refinery_id']} has {tokens}"
            )
    return problems


def upsert_refinery(conn: Conn, row: Row) -> str:
    """Returns 'inserted', 'updated' or 'unchanged'. Logs a status change."""
    found = conn.execute("SELECT * FROM refinery WHERE id = %(id)s", row).fetchone()
    if found is None:
        conn.execute(
            "INSERT INTO refinery (id, name, aliases, country, region, owner, nameplate_kbd,"
            " status, source_url, verified, verified_on, notes)"
            " VALUES (%(id)s, %(name)s, %(aliases)s, %(country)s, %(region)s, %(owner)s,"
            " %(nameplate_kbd)s, %(status)s, %(source_url)s, %(verified)s, %(verified_on)s,"
            " %(notes)s)",
            row,
        )
        return "inserted"
    if all(found[c] == row[c] for c in REFINERY_VALUE_COLUMNS):
        return "unchanged"
    conn.execute(
        "UPDATE refinery SET name = %(name)s, aliases = %(aliases)s, country = %(country)s,"
        " region = %(region)s, owner = %(owner)s, nameplate_kbd = %(nameplate_kbd)s,"
        " status = %(status)s, source_url = %(source_url)s, verified = %(verified)s,"
        " verified_on = %(verified_on)s, notes = %(notes)s, loaded_at = now()"
        " WHERE id = %(id)s",
        row,
    )
    if found["status"] != row["status"]:
        conn.execute(
            "INSERT INTO refinery_status_change (refinery_id, old_status, new_status)"
            " VALUES (%s, %s, %s)",
            (row["id"], found["status"], row["status"]),
        )
    return "updated"


def add_units(conn: Conn, refinery_id: str, codes: Sequence[str]) -> int:
    """Inserts missing unit rows with an empty tag. Returns how many were new."""
    added = 0
    for code in codes:
        cur = conn.execute(
            "INSERT INTO refinery_unit (refinery_id, unit_code, unit_tag)"
            " VALUES (%s, %s, '') ON CONFLICT (refinery_id, unit_code, unit_tag) DO NOTHING",
            (refinery_id, code),
        )
        added += cur.rowcount
    return added


def load_refineries(conn: Conn, path: Path) -> LoadResult:
    rows = read_rows(path, REFINERY_COLUMNS)
    problems = bad_unit_errors(path, rows)
    if problems:
        raise SeedError("; ".join(problems))
    parsed = [(line, parse_refinery(path, line, raw)) for line, raw in rows]
    outcomes: list[str] = []
    with conn.transaction():
        for line, row in parsed:
            try:
                with conn.transaction():
                    outcome = upsert_refinery(conn, row)
                    if add_units(conn, row["id"], row["units"]) and outcome == "unchanged":
                        outcome = "updated"
                    outcomes.append(outcome)
            except psycopg.Error as exc:
                raise database_error(path, line, exc) from exc
        missing = missing_ids(conn, "refinery", [row["id"] for _, row in parsed])
    return tally(outcomes, missing)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="refinery_watch.db.seed")
    commands = parser.add_subparsers(dest="command", required=True)
    sources = commands.add_parser("sources")
    sources.add_argument("--csv", type=Path, default=Path("data/sources.csv"))
    commands.add_parser("refineries")
    args = parser.parse_args(argv)
    try:
        path = args.csv if args.command == "sources" else refineries_csv_path()
        loader = load_sources if args.command == "sources" else load_refineries
        with connect(app_database_url()) as conn:
            result = loader(conn, path)
            conn.commit()
    except (ConfigError, SeedError, OSError) as exc:
        sys.stderr.write(f"seed: {exc}\n")
        return 2
    sys.stdout.write(f"{result}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
