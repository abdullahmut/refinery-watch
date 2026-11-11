from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from refinery_watch.config import ConfigError
from refinery_watch.db import migrate
from refinery_watch.db.connect import connect

pytestmark = pytest.mark.integration


def head() -> str | None:
    config = migrate.alembic_config("postgresql://unused/db")
    return ScriptDirectory.from_config(config).get_current_head()


def current(url: str, schema: str) -> str | None:
    with connect(url) as conn:
        exists = conn.execute(
            "SELECT to_regclass(%s) IS NOT NULL AS e", (f"{schema}.alembic_version",)
        ).fetchone()
        assert exists is not None
        if not exists["e"]:
            return None
        row = conn.execute(f"SELECT version_num FROM {schema}.alembic_version").fetchone()
        return None if row is None else str(row["version_num"])


def test_upgrade_reaches_head_and_is_repeatable(pg_server: str, fresh_schema: str) -> None:
    assert current(pg_server, fresh_schema) == head()
    migrate.upgrade(pg_server, app_role="refinery_test_app", schema=fresh_schema)
    assert current(pg_server, fresh_schema) == head()


@pytest.mark.parametrize("bad", ["x; drop", "Role", "1abc", "", "a-b", 'a"b'])
def test_bad_app_role_refused_before_connecting(bad: str) -> None:
    with pytest.raises(ValueError, match="app_role"):
        migrate.upgrade("postgresql://nobody@127.0.0.1:1/none", app_role=bad)


@pytest.mark.parametrize("bad", ["public; drop", "Public", "a b"])
def test_bad_schema_refused_before_connecting(bad: str) -> None:
    with pytest.raises(ValueError, match="schema"):
        migrate.upgrade("postgresql://nobody@127.0.0.1:1/none", schema=bad)


def test_config_has_no_ini_and_points_at_migrations() -> None:
    config = migrate.alembic_config("postgresql://u@127.0.0.1/db", "refinery_test_app", "s1")
    assert config.config_file_name is None
    assert Path(str(config.get_main_option("script_location"))).name == "migrations"
    assert config.attributes["app_role"] == "refinery_test_app"
    assert config.attributes["schema"] == "s1"


def test_template_downgrade_is_forward_only() -> None:
    template = (migrate.MIGRATIONS_DIR / "script.py.mako").read_text(encoding="utf-8")
    assert 'raise NotImplementedError("forward-only")' in template


def test_main_unset_database_url_names_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert migrate.main([]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_main_bad_role_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    assert migrate.main(["--app-role", "x; drop"]) == 2
    assert "app_role" in capsys.readouterr().err


def test_main_runs_upgrade(
    monkeypatch: pytest.MonkeyPatch, pg_server: str, fresh_schema: str
) -> None:
    calls: list[tuple[str, str]] = []

    def fake(url: str, app_role: str = "", schema: str = "") -> None:
        calls.append((url, app_role))

    monkeypatch.setenv("DATABASE_URL", pg_server)
    monkeypatch.setattr(migrate, "upgrade", fake)
    assert migrate.main(["--app-role", "refinery_test_app"]) == 0
    assert calls == [(pg_server, "refinery_test_app")]


def test_config_error_type() -> None:
    assert issubclass(ConfigError, RuntimeError)
    assert psycopg  # driver importable


REVISION = """
from alembic import op
from refinery_watch.db.migrations.params import app_role, grant, revoke, schema

revision = "t001"
down_revision = None


def upgrade() -> None:
    op.execute("CREATE TABLE probe (id int PRIMARY KEY, schema_name text)")
    op.execute(grant("SELECT, INSERT, UPDATE", "probe"))
    op.execute(revoke("UPDATE", "probe"))
    op.execute("INSERT INTO probe VALUES (1, '" + schema() + ":" + app_role() + "')")


def downgrade() -> None:
    raise NotImplementedError("forward-only")
"""


def probe_config(tmp_path: Path, url: str, schema: str) -> Config:
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "t001_probe.py").write_text(REVISION, encoding="utf-8")
    config = migrate.alembic_config(url, "refinery_test_app", schema)
    config.set_main_option("version_locations", str(versions))
    return config


def test_env_uses_schema_and_parameters(tmp_path: Path, pg_server: str, fresh_schema: str) -> None:
    command.upgrade(probe_config(tmp_path, pg_server, fresh_schema), "head")
    assert current(pg_server, fresh_schema) == "t001"
    with connect(pg_server) as conn:
        row = conn.execute(f"SELECT schema_name FROM {fresh_schema}.probe").fetchone()
        assert row == {"schema_name": f"{fresh_schema}:refinery_test_app"}
        privs = conn.execute(
            "SELECT has_table_privilege('refinery_test_app', %s, 'SELECT') AS s,"
            " has_table_privilege('refinery_test_app', %s, 'UPDATE') AS u,"
            " has_table_privilege('refinery_test_app', %s, 'SELECT') AS v",
            (f"{fresh_schema}.probe", f"{fresh_schema}.probe", f"{fresh_schema}.alembic_version"),
        ).fetchone()
    assert privs == {"s": True, "u": False, "v": False}


def test_downgrade_raises(tmp_path: Path, pg_server: str, fresh_schema: str) -> None:
    config = probe_config(tmp_path, pg_server, fresh_schema)
    command.upgrade(config, "head")
    with pytest.raises(NotImplementedError, match="forward-only"):
        command.downgrade(config, "-1")
