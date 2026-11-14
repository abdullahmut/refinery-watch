"""Every function pins its search_path, so a temp table cannot shadow a real one."""

import importlib
from collections.abc import Iterator
from urllib.parse import urlparse

import psycopg
import psycopg.sql
import pytest
from tests.db.helpers import add_document, open_in_schema, unit_id

from refinery_watch.db.connect import Conn

pytestmark = pytest.mark.integration

VERSIONS = "refinery_watch.db.migrations.versions"
MIGRATIONS = (
    "r0003_outage_event_tables",
    "r0004_outage_as_of",
    "r0005_insert_rules",
    "r0007_dedupe_functions",
    "r0008_note_price_tceq",
)

INSERT = (
    "INSERT INTO {s}.outage_event (event_type, refinery_id, refinery_unit_id, capacity_basis,"
    " start_date, confidence, match_score) VALUES ('unplanned', 'R9001', %s, 'not_stated',"
    " %s, 'low', 90) RETURNING id"
)
SOURCE = (
    "INSERT INTO {s}.event_source (outage_event_id, raw_document_id, role, source_kind, evidence)"
    " VALUES (%s, %s, 'primary', 'other_media', 'q')"
)


def pinned_signatures() -> set[str]:
    """Every signature the migrations pin, so a new function without a pin fails the count."""
    found: set[str] = set()
    for module in MIGRATIONS:
        found.update(importlib.import_module(f"{VERSIONS}.{module}").PINNED)
    return found


def test_every_function_pins_search_path_with_pg_temp_last(db: Conn, session_schema: str) -> None:
    rows = db.execute(
        "SELECT p.proname, p.proconfig FROM pg_proc p WHERE p.pronamespace = %s::regnamespace"
        " AND p.prokind = 'f'",
        (session_schema,),
    ).fetchall()
    assert len(rows) == len(pinned_signatures())
    expected = f"search_path=pg_catalog, {session_schema}, pg_temp"
    for row in rows:
        assert expected in row["proconfig"], row["proname"]


@pytest.fixture
def temp_granted(pg_server: str, app_url: str) -> Iterator[None]:
    role = psycopg.sql.Identifier(urlparse(app_url).username or "")
    database = psycopg.sql.Identifier(urlparse(pg_server).path.lstrip("/"))
    grant = psycopg.sql.SQL("GRANT TEMPORARY ON DATABASE {} TO {}").format(database, role)
    revoke = psycopg.sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM {}").format(database, role)
    with psycopg.connect(pg_server, autocommit=True) as owner:
        owner.execute(grant)
        try:
            yield
        finally:
            owner.execute(revoke)


def test_temp_table_cannot_shadow_outage_event(
    temp_granted: None, session_schema: str, app_url: str
) -> None:
    """A temp table named outage_event must not hide the real table from the trigger."""
    conn = open_in_schema(app_url, session_schema)
    try:
        cdu = unit_id(conn)
        conn.execute(f"CREATE TEMP TABLE outage_event (LIKE {session_schema}.outage_event)")
        first = conn.execute(INSERT.format(s=session_schema), (cdu, "2025-11-01")).fetchone()
        assert first is not None
        conn.execute(SOURCE.format(s=session_schema), (first["id"], add_document(conn, 801)))
        with pytest.raises(psycopg.Error) as info, conn.transaction():
            conn.execute(INSERT.format(s=session_schema), (cdu, "2025-11-02"))
        assert info.value.sqlstate == "RW006"
        conn.execute("DROP TABLE pg_temp.outage_event")
        count = conn.execute(
            "SELECT count(*) AS n FROM outage_as_of(clock_timestamp())"
            " WHERE refinery_id = 'R9001' AND refinery_unit_id = %s",
            (cdu,),
        ).fetchone()
        assert count == {"n": 1}
    finally:
        conn.rollback()
        conn.close()
