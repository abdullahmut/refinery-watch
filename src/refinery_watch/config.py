"""Settings read from the environment. Nothing here reads a .env file."""

import os
from pathlib import Path


class ConfigError(RuntimeError):
    """A required setting is missing or invalid."""


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def database_url() -> str:
    return _required("DATABASE_URL")


def refineries_csv_path() -> Path:
    path = Path(_required("REFINERIES_CSV_PATH"))
    if not path.is_file():
        raise ConfigError(f"REFINERIES_CSV_PATH is not a file: {path}")
    return path
