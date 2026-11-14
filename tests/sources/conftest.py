"""Optional real fixtures. Absent files skip the real-fixture tests, or fail them on request."""

import os
from collections.abc import Callable
from pathlib import Path

import pytest

DEFAULT_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "sources"


def fixtures_dir() -> Path:
    override = os.environ.get("RW_FIXTURES_DIR")
    return Path(override) if override else DEFAULT_DIR


def find_fixture(pattern: str) -> Path | None:
    """Newest file under the fixtures folder matching the glob, or None.

    Files with _derived_ in the name match only a pattern that names them.
    """
    found = list(fixtures_dir().glob(pattern))
    if "_derived_" not in pattern:
        found = [p for p in found if "_derived_" not in p.name]
    if not found:
        return None
    return max(found, key=lambda p: (p.stat().st_mtime, p.name))


@pytest.fixture
def real_fixture() -> Callable[[str], Path]:
    def get(pattern: str) -> Path:
        path = find_fixture(pattern)
        if path is not None:
            return path
        message = f"real fixture absent: {pattern}"
        if os.environ.get("REQUIRE_REAL_FIXTURES") == "1":
            pytest.fail(message)
        pytest.skip(message)

    return get
