"""Runs the migrations. Operator command: python -m refinery_watch.db.migrate."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from alembic import command
from alembic.config import Config

from refinery_watch.config import ConfigError, owner_database_url
from refinery_watch.db.migrations.params import check_identifier

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(
    database_url: str, app_role: str = "refinery_app", schema: str = "public"
) -> Config:
    check_identifier(app_role, "app_role")
    check_identifier(schema, "schema")
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("path_separator", "os")
    config.attributes["database_url"] = database_url
    config.attributes["app_role"] = app_role
    config.attributes["schema"] = schema
    return config


def upgrade(
    database_url: str,
    app_role: str = "refinery_app",
    schema: str = "public",
    revision: str = "head",
) -> None:
    command.upgrade(alembic_config(database_url, app_role, schema), revision)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="refinery_watch.db.migrate")
    parser.add_argument("--app-role", default="refinery_app")
    args = parser.parse_args(argv)
    try:
        upgrade(owner_database_url(), app_role=args.app_role)
    except (ConfigError, ValueError) as exc:
        sys.stderr.write(f"migrate: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
