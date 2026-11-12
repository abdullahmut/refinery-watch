import re
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from refinery_watch.db import seed
from refinery_watch.db.connect import Conn
from refinery_watch.db.seed import LoadResult, SeedError, load_refineries, load_sources

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "seed"
HEADER = (FIXTURES / "sources_small.csv").read_text(encoding="utf-8").splitlines()[0]


def source_ids(conn: Conn) -> list[str]:
    return [r["id"] for r in conn.execute("SELECT id FROM source ORDER BY id").fetchall()]


def write_sources(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "sources.csv"
    path.write_text("\n".join([HEADER, *lines]) + "\n", encoding="utf-8")
    return path


GOOD = "s1,Name,2,Global,rss,https://a.test/f,https://a.test/<id>,open,60,note,no"


def test_sources_fixture_inserts_then_is_unchanged(fresh_db: Conn) -> None:
    path = FIXTURES / "sources_small.csv"
    assert load_sources(fresh_db, path) == LoadResult(3, 0, 0, ())
    assert load_sources(fresh_db, path) == LoadResult(0, 0, 3, ())
    assert source_ids(fresh_db) == ["test_agency", "test_press", "test_wire"]


def test_sources_changed_name_is_one_update(fresh_db: Conn, tmp_path: Path) -> None:
    text = (FIXTURES / "sources_small.csv").read_text(encoding="utf-8")
    load_sources(fresh_db, FIXTURES / "sources_small.csv")
    edited = tmp_path / "edited.csv"
    edited.write_text(text.replace("Test Wire", "Test Wire Ltd"), encoding="utf-8")
    assert load_sources(fresh_db, edited) == LoadResult(0, 1, 2, ())
    row = fresh_db.execute("SELECT name FROM source WHERE id = 'test_wire'").fetchone()
    assert row == {"name": "Test Wire Ltd"}


def test_sources_values_are_typed_and_notes_default_empty(fresh_db: Conn) -> None:
    load_sources(fresh_db, FIXTURES / "sources_small.csv")
    row = fresh_db.execute(
        "SELECT tier, fetch_interval_min, notes, verified FROM source WHERE id = 'test_press'"
    ).fetchone()
    assert row == {"tier": 3, "fetch_interval_min": 360, "notes": "", "verified": False}
    agency = fresh_db.execute(
        "SELECT verified, robots_status FROM source WHERE id = 'test_agency'"
    ).fetchone()
    assert agency == {"verified": True, "robots_status": "api_key_required"}


def test_sources_missing_from_file_are_reported_not_deleted(fresh_db: Conn, tmp_path: Path) -> None:
    load_sources(fresh_db, FIXTURES / "sources_small.csv")
    result = load_sources(fresh_db, write_sources(tmp_path, GOOD))
    assert result == LoadResult(1, 0, 0, ("test_agency", "test_press", "test_wire"))
    assert len(source_ids(fresh_db)) == 4


@pytest.mark.parametrize(
    ("line", "column"),
    [
        (GOOD.replace(",no", ",maybe"), "verified"),
        (GOOD.replace(",2,", ",two,"), "tier"),
        (GOOD.replace(",60,", ",soon,"), "fetch_interval_min"),
    ],
)
def test_sources_bad_value_names_line_and_column(
    fresh_db: Conn, tmp_path: Path, line: str, column: str
) -> None:
    path = write_sources(tmp_path, line)
    with pytest.raises(SeedError, match=rf"sources\.csv:2: column {column}"):
        load_sources(fresh_db, path)


def test_sources_database_check_names_line(fresh_db: Conn, tmp_path: Path) -> None:
    path = write_sources(tmp_path, GOOD, GOOD.replace("s1,", "s2,").replace(",rss,", ",fax,"))
    with pytest.raises(SeedError, match=r"sources\.csv:3: column"):
        load_sources(fresh_db, path)
    assert source_ids(fresh_db) == []


def test_sources_one_bad_row_loads_nothing(fresh_db: Conn, tmp_path: Path) -> None:
    path = write_sources(tmp_path, GOOD, GOOD.replace("s1,", "s2,").replace(",no", ",x"))
    with pytest.raises(SeedError):
        load_sources(fresh_db, path)
    assert source_ids(fresh_db) == []


def test_sources_wrong_header_and_short_row(fresh_db: Conn, tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("id,name\n1,2\n", encoding="utf-8")
    with pytest.raises(SeedError, match="header"):
        load_sources(fresh_db, bad)
    with pytest.raises(SeedError, match=r"sources\.csv:2: column row"):
        load_sources(fresh_db, write_sources(tmp_path, "s1,Name"))
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(SeedError, match="header"):
        load_sources(fresh_db, empty)


def test_sources_multiline_cell_keeps_line_numbers(fresh_db: Conn, tmp_path: Path) -> None:
    first = GOOD.replace(",note,", ',"two\nlines",')
    path = write_sources(tmp_path, first, GOOD.replace("s1,", "s2,").replace(",no", ",x"))
    with pytest.raises(SeedError, match=r"sources\.csv:4: column verified"):
        load_sources(fresh_db, path)


def test_real_source_list_loads(fresh_db: Conn) -> None:
    assert load_sources(fresh_db, ROOT / "data" / "sources.csv").inserted == 24


def schema_connector(url: str, schema: str) -> Callable[[str], Conn]:
    def make(_: str) -> Conn:
        return psycopg.connect(url, row_factory=dict_row, options=f"-c search_path={schema}")

    return make


def test_main_sources(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pg_server: str,
    fresh_schema: str,
    fresh_db: Conn,
) -> None:
    monkeypatch.setenv("OWNER_DATABASE_URL", "postgresql://unused@127.0.0.1/none")
    monkeypatch.setattr(seed, "connect", schema_connector(pg_server, fresh_schema))
    assert seed.main(["sources", "--csv", str(FIXTURES / "sources_small.csv")]) == 0
    assert "inserted=3" in capsys.readouterr().out
    assert len(source_ids(fresh_db)) == 3


def test_main_errors_exit_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pg_server: str,
    fresh_schema: str,
) -> None:
    monkeypatch.delenv("OWNER_DATABASE_URL", raising=False)
    monkeypatch.delenv("PG_HOST", raising=False)
    monkeypatch.delenv("PG_OWNER_PASSWORD", raising=False)
    assert seed.main(["sources"]) == 2
    assert "PG_HOST" in capsys.readouterr().err
    monkeypatch.setenv("OWNER_DATABASE_URL", "postgresql://unused@127.0.0.1/none")
    monkeypatch.setattr(seed, "connect", schema_connector(pg_server, fresh_schema))
    assert seed.main(["sources", "--csv", "no/such/file.csv"]) == 2
    assert "seed:" in capsys.readouterr().err


def refinery_ids(conn: Conn) -> list[str]:
    return [r["id"] for r in conn.execute("SELECT id FROM refinery ORDER BY id").fetchall()]


def unit_codes(conn: Conn, refinery_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT unit_code FROM refinery_unit WHERE refinery_id = %s ORDER BY unit_code",
        (refinery_id,),
    ).fetchall()
    return [r["unit_code"] for r in rows]


def edit_fixture(tmp_path: Path, old: str, new: str) -> Path:
    text = (FIXTURES / "refineries_small.csv").read_text(encoding="utf-8")
    assert old in text
    out = tmp_path / "refineries_edited.csv"
    out.write_text(text.replace(old, new), encoding="utf-8")
    return out


def status_changes(conn: Conn) -> list[dict[str, str]]:
    rows = conn.execute(
        "SELECT refinery_id, old_status, new_status FROM refinery_status_change ORDER BY id"
    ).fetchall()
    return list(rows)


def test_refineries_fixture_inserts_then_is_unchanged(fresh_db: Conn) -> None:
    path = FIXTURES / "refineries_small.csv"
    assert load_refineries(fresh_db, path) == LoadResult(3, 0, 0, ())
    assert load_refineries(fresh_db, path) == LoadResult(0, 0, 3, ())
    assert refinery_ids(fresh_db) == ["R9001", "R9002", "R9003"]


def test_refineries_units_and_aliases(fresh_db: Conn) -> None:
    load_refineries(fresh_db, FIXTURES / "refineries_small.csv")
    assert unit_codes(fresh_db, "R9001") == ["CDU", "FCC", "VDU"]
    assert unit_codes(fresh_db, "R9002") == ["CDU"]
    assert unit_codes(fresh_db, "R9003") == ["CDU", "HCU"]
    tags = fresh_db.execute("SELECT DISTINCT unit_tag FROM refinery_unit").fetchall()
    assert tags == [{"unit_tag": ""}]
    row = fresh_db.execute(
        "SELECT aliases, nameplate_kbd, owner, verified, verified_on"
        " FROM refinery WHERE id = 'R9003'"
    ).fetchone()
    assert row is not None
    assert row["aliases"] == ["Samplebay", "Sample Bay Works"]
    assert row["nameplate_kbd"] is None
    assert row["owner"] is None
    assert row["verified"] is True
    assert str(row["verified_on"]) == "2026-01-15"
    plain = fresh_db.execute("SELECT aliases FROM refinery WHERE id = 'R9001'").fetchone()
    assert plain == {"aliases": []}


def test_refinery_status_change_is_logged_once(fresh_db: Conn, tmp_path: Path) -> None:
    load_refineries(fresh_db, FIXTURES / "refineries_small.csv")
    assert status_changes(fresh_db) == []
    edited = edit_fixture(tmp_path, ",150,CDU,closed_or_converted", ",150,CDU,operating")
    assert load_refineries(fresh_db, edited) == LoadResult(0, 1, 2, ())
    assert status_changes(fresh_db) == [
        {"refinery_id": "R9002", "old_status": "closed_or_converted", "new_status": "operating"}
    ]
    assert load_refineries(fresh_db, edited) == LoadResult(0, 0, 3, ())
    assert len(status_changes(fresh_db)) == 1


def test_refinery_other_change_logs_no_status_row(fresh_db: Conn, tmp_path: Path) -> None:
    load_refineries(fresh_db, FIXTURES / "refineries_small.csv")
    edited = edit_fixture(tmp_path, "Test Oil Co", "Test Oil plc")
    assert load_refineries(fresh_db, edited).updated == 1
    assert status_changes(fresh_db) == []


def test_refinery_new_unit_is_added_never_removed(fresh_db: Conn, tmp_path: Path) -> None:
    load_refineries(fresh_db, FIXTURES / "refineries_small.csv")
    more = edit_fixture(tmp_path, "CDU;VDU;FCC", "CDU;VDU;FCC;COK")
    assert load_refineries(fresh_db, more) == LoadResult(0, 1, 2, ())
    assert unit_codes(fresh_db, "R9001") == ["CDU", "COK", "FCC", "VDU"]
    fewer = edit_fixture(tmp_path, "CDU;VDU;FCC", "CDU")
    assert load_refineries(fresh_db, fewer) == LoadResult(0, 0, 3, ())
    assert unit_codes(fresh_db, "R9001") == ["CDU", "COK", "FCC", "VDU"]


def test_refinery_missing_from_file_reported_not_deleted(fresh_db: Conn, tmp_path: Path) -> None:
    load_refineries(fresh_db, FIXTURES / "refineries_small.csv")
    lines = (FIXTURES / "refineries_small.csv").read_text(encoding="utf-8").splitlines()
    short = tmp_path / "short.csv"
    short.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
    result = load_refineries(fresh_db, short)
    assert result == LoadResult(0, 0, 1, ("R9002", "R9003"))
    assert refinery_ids(fresh_db) == ["R9001", "R9002", "R9003"]


def test_refinery_bad_units_named_and_nothing_loaded(fresh_db: Conn) -> None:
    with pytest.raises(SeedError) as info:
        load_refineries(fresh_db, FIXTURES / "refineries_bad_units.csv")
    assert "refineries_bad_units.csv:2" in str(info.value)
    assert "major_units" in str(info.value)
    assert refinery_ids(fresh_db) == []


def test_refinery_bad_units_all_rows_named(fresh_db: Conn, tmp_path: Path) -> None:
    lines = (FIXTURES / "refineries_small.csv").read_text(encoding="utf-8").splitlines()
    bad = [
        lines[0],
        lines[1].replace("CDU;VDU;FCC", "CDU 155.1 kb/d per press report; check the site"),
        lines[2],
        lines[3].replace("HCU;CDU", "RFCC"),
    ]
    path = tmp_path / "r.csv"
    path.write_text("\n".join(bad) + "\n", encoding="utf-8")
    with pytest.raises(SeedError) as info:
        load_refineries(fresh_db, path)
    message = str(info.value)
    assert "r.csv:2" in message
    assert "r.csv:4" in message
    assert "R9001" in message
    assert "R9003" in message
    assert "RFCC" in message


@pytest.mark.parametrize(
    ("old", "new", "column"),
    [
        (",no,,Invented plant with three", ",maybe,,Invented plant with three", "verified"),
        (",200,CDU;VDU", ",lots,CDU;VDU", "nameplate_kbd"),
        (",yes,2026-01-15,", ",yes,15 Jan,", "verified_on"),
    ],
)
def test_refinery_bad_value_names_column(
    fresh_db: Conn, tmp_path: Path, old: str, new: str, column: str
) -> None:
    path = edit_fixture(tmp_path, old, new)
    with pytest.raises(SeedError, match=f"column {column}"):
        load_refineries(fresh_db, path)


def test_refinery_database_check_names_line(fresh_db: Conn, tmp_path: Path) -> None:
    path = edit_fixture(tmp_path, "NW Europe", "Narnia")
    with pytest.raises(SeedError, match=r"refineries_edited\.csv:3: column"):
        load_refineries(fresh_db, path)
    assert refinery_ids(fresh_db) == []


def test_unit_codes_constant_matches_database(db: Conn) -> None:
    row = db.execute(
        "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint"
        " WHERE conrelid = 'refinery_unit'::regclass AND contype = 'c'"
        " AND pg_get_constraintdef(oid) ~ 'unit_code'"
    ).fetchone()
    assert row is not None
    assert set(re.findall(r"'([A-Z0-9_]+)'", row["d"])) == set(seed.UNIT_CODES)


def test_main_refineries(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pg_server: str,
    fresh_schema: str,
    fresh_db: Conn,
) -> None:
    monkeypatch.setenv("OWNER_DATABASE_URL", "postgresql://unused@127.0.0.1/none")
    monkeypatch.setenv("REFINERIES_CSV_PATH", str(FIXTURES / "refineries_small.csv"))
    monkeypatch.setattr(seed, "connect", schema_connector(pg_server, fresh_schema))
    assert seed.main(["refineries"]) == 0
    assert "inserted=3" in capsys.readouterr().out
    assert refinery_ids(fresh_db) == ["R9001", "R9002", "R9003"]


def test_main_refineries_without_path_names_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OWNER_DATABASE_URL", "postgresql://unused@127.0.0.1/none")
    monkeypatch.delenv("REFINERIES_CSV_PATH", raising=False)
    assert seed.main(["refineries"]) == 2
    assert "REFINERIES_CSV_PATH" in capsys.readouterr().err


def test_app_role_cannot_insert_refinery(app_db: Conn) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as info:
        app_db.execute(
            "INSERT INTO refinery (id, name, country, region, status)"
            " VALUES ('R9100', 'X', 'US', 'US Gulf', 'operating')"
        )
    assert info.value.sqlstate == "42501"


def test_refineries_csv_is_not_in_the_repo() -> None:
    assert not (ROOT / "data" / "refineries.csv").exists()
