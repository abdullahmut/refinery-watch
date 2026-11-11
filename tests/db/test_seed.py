from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from refinery_watch.db import seed
from refinery_watch.db.connect import Conn
from refinery_watch.db.seed import LoadResult, SeedError, load_sources

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
    assert load_sources(fresh_db, ROOT / "data" / "sources.csv").inserted == 21


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
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused@127.0.0.1/none")
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
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert seed.main(["sources"]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused@127.0.0.1/none")
    monkeypatch.setattr(seed, "connect", schema_connector(pg_server, fresh_schema))
    assert seed.main(["sources", "--csv", "no/such/file.csv"]) == 2
    assert "seed:" in capsys.readouterr().err
