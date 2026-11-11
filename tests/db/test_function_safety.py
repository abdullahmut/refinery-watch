"""Every function pins its search_path, so a temp table cannot shadow a real one."""

from collections.abc import Iterator

import psycopg
import pytest
from tests.db.helpers import add_document, open_in_schema, unit_id

from refinery_watch.db.connect import Conn

pytestmark = pytest.mark.integration

INSERT = (
    "INSERT INTO {s}.outage_event (event_type, refinery_id, refinery_unit_id, capacity_basis,"
    " start_date, confidence, match_score) VALUES ('unplanned', 'R9001', %s, 'not_stated',"
    " %s, 'low', 90) RETURNING id"
)
SOURCE = (
    "INSERT INTO {s}.event_source (outage_event_id, raw_document_id, role, source_kind, evidence)"
    " VALUES (%s, %s, 'primary', 'other_media', 'q')"
)


def test_every_function_pins_search_path_with_pg_temp_last(db: Conn, session_schema: str) -> None:
    rows = db.execute(
        "SELECT p.proname, p.proconfig FROM pg_proc p WHERE p.pronamespace = %s::regnamespace"
        " AND p.prokind = 'f'",
        (session_schema,),
    ).fetchall()
    assert len(rows) == 10
    expected = f"search_path=pg_catalog, {session_schema}, pg_temp"
    for row in rows:
        assert row["proconfig"] == [expected], row["proname"]


@pytest.fixture
def temp_granted(pg_server: str, app_url: str) -> Iterator[None]:
    role = psycopg.conninfo.conninfo_to_dict(app_url)["user"]
    with psycopg.connect(pg_server, autocommit=True) as owner:
        owner.execute(f"GRANT TEMPORARY ON DATABASE refinery_watch_test TO {role}")
        try:
            yield
        finally:
            owner.execute(f"REVOKE TEMPORARY ON DATABASE refinery_watch_test FROM {role}")


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
