from pathlib import Path

import pytest

from refinery_watch.config import ConfigError, database_url, refineries_csv_path


def test_database_url_unset_names_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        database_url()


def test_database_url_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x@127.0.0.1/db")
    assert database_url() == "postgresql://x@127.0.0.1/db"


def test_refineries_csv_path_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REFINERIES_CSV_PATH", raising=False)
    with pytest.raises(ConfigError, match="REFINERIES_CSV_PATH"):
        refineries_csv_path()


def test_refineries_csv_path_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("REFINERIES_CSV_PATH", str(tmp_path / "nope.csv"))
    with pytest.raises(ConfigError, match="REFINERIES_CSV_PATH"):
        refineries_csv_path()


def test_refineries_csv_path_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("REFINERIES_CSV_PATH", str(tmp_path))
    with pytest.raises(ConfigError):
        refineries_csv_path()


def test_refineries_csv_path_returned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "r.csv"
    f.write_text("x\n")
    monkeypatch.setenv("REFINERIES_CSV_PATH", str(f))
    assert refineries_csv_path() == f
