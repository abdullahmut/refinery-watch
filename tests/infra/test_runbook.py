"""Checks that runbook commands keep secrets out of command lines."""

import re
from pathlib import Path
from typing import Final

RUNBOOK: Final[Path] = Path(__file__).resolve().parents[2] / "docs" / "runbook.md"


def lines() -> list[str]:
    """Return the runbook lines."""
    return RUNBOOK.read_text(encoding="utf-8").splitlines()


def test_no_psql_command_takes_a_variable_with_dash_c() -> None:
    for line in lines():
        if ":'" in line:
            assert not re.search(r"(^|\s)-c(\s|=|$)", line), line
        assert not re.search(r"PASSWORD\s+'", line), line


def test_psql_runs_in_the_container_not_on_the_host() -> None:
    for line in lines():
        assert not re.match(r"\s*(PGPASSWORD=\S+\s+)?psql\s", line), line


def test_rotation_passes_the_password_by_environment() -> None:
    text = "\n".join(lines())
    section = text.split("## Rotate a database password", 1)[1].split("\n## ", 1)[0]
    assert "(\nset -a; . ./.env; set +a\n" in section
    assert "SQL\n)\n" in section
    assert "exec -T -e PG_APP_PASSWORD -u postgres postgres psql" in section
    assert "\\getenv pw PG_APP_PASSWORD" in section
    assert "SET log_min_error_statement TO panic;" in section
    assert "read -rs PGPASSWORD && export PGPASSWORD" in section
    assert "unset PGPASSWORD" in section
