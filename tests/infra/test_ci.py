"""Checks the CI workflow by parsing it. No network."""

import re
from pathlib import Path
from typing import Any, Final

import yaml

ROOT: Final[Path] = Path(__file__).resolve().parents[2]
CI_TEXT: Final[str] = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
CHECKS: Final[dict[str, Any]] = yaml.safe_load(CI_TEXT)["jobs"]["checks"]
COMPOSE: Final[dict[str, Any]] = yaml.safe_load(
    (ROOT / "infra" / "compose" / "data" / "compose.yaml").read_text(encoding="utf-8")
)


def run_steps() -> list[str]:
    return [str(s["run"]) for s in CHECKS["steps"] if "run" in s]


def test_service_image_matches_compose() -> None:
    assert CHECKS["services"]["postgres"]["image"] == COMPOSE["services"]["postgres"]["image"]


def test_service_binds_loopback_only() -> None:
    assert CHECKS["services"]["postgres"]["ports"] == ["127.0.0.1:5432:5432"]


def test_test_roles_step_reads_roles_script() -> None:
    steps = run_steps()
    roles = [s for s in steps if "20-test-roles.sql" in s]
    assert len(roles) == 1
    assert "-v ON_ERROR_STOP=1" in roles[0]


def test_step_order() -> None:
    steps = run_steps()
    order = [
        steps.index("make lint"),
        steps.index("make test"),
        next(i for i, s in enumerate(steps) if "20-test-roles.sql" in s),
        steps.index("make test-integration"),
    ]
    assert order == sorted(order)


def test_no_secrets_and_pinned_actions() -> None:
    assert "secrets." not in CI_TEXT
    uses = re.findall(r"uses: \S+@(\S+)", CI_TEXT)
    assert uses
    assert all(re.fullmatch(r"[0-9a-f]{40}", u) for u in uses)


def test_timeout_unchanged() -> None:
    assert CHECKS["timeout-minutes"] == 10
