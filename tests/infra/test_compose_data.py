"""Checks the data Droplet Compose files by parsing them. No Docker, no network."""

import re
from pathlib import Path
from typing import Any, Final

import yaml

ROOT: Final[Path] = Path(__file__).resolve().parents[2]
COMPOSE_DIR: Final[Path] = ROOT / "infra" / "compose" / "data"
EXPECTED_VARS: Final[frozenset[str]] = frozenset(
    {
        "POSTGRES_PASSWORD",
        "PG_OWNER_PASSWORD",
        "PG_APP_PASSWORD",
        "MINIO_ROOT_USER",
        "MINIO_ROOT_PASSWORD",
        "MINIO_APP_ACCESS_KEY",
        "MINIO_APP_SECRET_KEY",
        "DATA_BIND_ADDR",
    }
)
MEM_LIMITS_MIB: Final[dict[str, int]] = {"postgres": 384}
VOLUMES: Final[frozenset[str]] = frozenset({"pgdata"})
SERVICES: Final[frozenset[str]] = frozenset(MEM_LIMITS_MIB)
FILES: Final[tuple[str, ...]] = ("compose.yaml",)
PG_SETTINGS: Final[dict[str, str]] = {
    "shared_buffers": "96MB",
    "work_mem": "4MB",
    "maintenance_work_mem": "32MB",
    "autovacuum_max_workers": "2",
    "autovacuum_work_mem": "16MB",
    "max_connections": "20",
    "max_parallel_workers_per_gather": "0",
    "effective_cache_size": "256MB",
    "timezone": "UTC",
    "log_timezone": "UTC",
    "hba_file": "/etc/postgresql/pg_hba.conf",
}
BACKEND_OVERHEAD_MIB: Final[int] = 6
TOTAL_LIMIT_MIB: Final[int] = 704
LOGGING: Final[dict[str, Any]] = {
    "driver": "json-file",
    "options": {"max-size": "10m", "max-file": "3"},
}
HEALTH_KEYS: Final[frozenset[str]] = frozenset(
    {"test", "interval", "timeout", "retries", "start_period"}
)
VAR_PATTERN: Final[re.Pattern[str]] = re.compile(r"\$\{([A-Za-z_]+)([^}]*)\}")


def load(name: str) -> dict[str, Any]:
    """Parse one YAML file in the data compose directory."""
    data = yaml.safe_load((COMPOSE_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def services(name: str) -> dict[str, dict[str, Any]]:
    """Return the services mapping of one compose file."""
    found = load(name).get("services", {})
    assert isinstance(found, dict)
    return found


def mib(value: str) -> int:
    """Parse sizes such as 384m, 96MB and 192MiB into MiB."""
    match = re.fullmatch(r"(\d+)(m|mb|mib)", value.strip().lower())
    assert match, value
    return int(match.group(1))


def pg_settings() -> dict[str, str]:
    """Return the -c key=value pairs of the postgres command."""
    command = services("compose.yaml")["postgres"]["command"]
    assert command[0] == "postgres"
    assert set(command[1::2]) == {"-c"}
    found: dict[str, str] = {}
    for item in command[2::2]:
        key, _, value = item.partition("=")
        found[key] = value
    return found


def test_services_volumes_and_name() -> None:
    data = load("compose.yaml")
    assert data["name"] == "refinery-watch-data"
    assert "version" not in data
    assert set(data["services"]) == SERVICES
    assert set(data["volumes"]) == VOLUMES
    assert all(not options for options in data["volumes"].values())


def test_restart_policies() -> None:
    for name, service in services("compose.yaml").items():
        assert service["restart"] == "always", name


def test_memory_limits() -> None:
    found = services("compose.yaml")
    for name, limit in MEM_LIMITS_MIB.items():
        assert found[name]["mem_limit"] == f"{limit}m"
        assert found[name]["memswap_limit"] == found[name]["mem_limit"]
    assert sum(mib(s["mem_limit"]) for s in found.values()) <= TOTAL_LIMIT_MIB


def test_healthchecks() -> None:
    check = services("compose.yaml")["postgres"]["healthcheck"]
    assert set(check) == HEALTH_KEYS
    assert check["test"][:2] == ["CMD", "pg_isready"]
    assert check["test"][check["test"].index("-h") + 1] == "127.0.0.1"
    assert check["start_period"] == "60s"


def test_postgres_settings_fit_limit() -> None:
    settings = pg_settings()
    assert settings == PG_SETTINGS
    limit = mib(services("compose.yaml")["postgres"]["mem_limit"])
    assert mib(settings["shared_buffers"]) * 4 <= limit
    worst = (
        mib(settings["shared_buffers"])
        + int(settings["max_connections"]) * (mib(settings["work_mem"]) + BACKEND_OVERHEAD_MIB)
        + int(settings["autovacuum_max_workers"]) * mib(settings["autovacuum_work_mem"])
        + mib(settings["maintenance_work_mem"])
    )
    assert worst < limit
    assert services("compose.yaml")["postgres"]["stop_grace_period"] == "60s"


def test_mounts_are_named_volumes_or_read_only_files() -> None:
    declared = set(load("compose.yaml")["volumes"])
    for name in FILES:
        for service_name, service in services(name).items():
            for mount in service.get("volumes", []):
                source, _, rest = mount.partition(":")
                if not source.startswith("."):
                    assert source in declared, (service_name, mount)
                    continue
                _, _, mode = rest.partition(":")
                assert mode == "ro", mount
                path = (COMPOSE_DIR / source).resolve()
                assert path.is_file(), mount
                assert path.is_relative_to(COMPOSE_DIR), mount


def test_no_env_file_or_privileges() -> None:
    for name in FILES:
        for service in services(name).values():
            assert "env_file" not in service
            assert "cap_add" not in service
            assert "network_mode" not in service
            assert not service.get("privileged", False)


def test_logging_and_no_new_privileges() -> None:
    for service in services("compose.yaml").values():
        assert service["logging"] == LOGGING
        assert service["security_opt"] == ["no-new-privileges:true"]


def test_base_file_publishes_nothing() -> None:
    for service in services("compose.yaml").values():
        assert "ports" not in service
        assert "expose" not in service


def test_interpolated_vars_are_known_and_required() -> None:
    for name in FILES:
        text = (COMPOSE_DIR / name).read_text(encoding="utf-8")
        for var, tail in VAR_PATTERN.findall(text):
            assert var in EXPECTED_VARS, var
            assert tail.startswith(":?"), (var, tail)


def test_image_tags_are_exact() -> None:
    image = services("compose.yaml")["postgres"]["image"]
    assert re.match(r"postgres:16\.\d+-bookworm(@sha256:[0-9a-f]{64})?$", image)
    assert "latest" not in image


def test_pg_hba_rejects_remote_superuser() -> None:
    path = COMPOSE_DIR / "postgres" / "pg_hba.conf"
    lines = [
        line.split()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    methods = {line[-1] for line in lines}
    assert not {"trust", "md5", "password"} & methods
    rejects = [i for i, line in enumerate(lines) if line[0] == "host" and line[-1] == "reject"]
    allows = [i for i, line in enumerate(lines) if line[0] == "host" and line[-1] != "reject"]
    assert len(rejects) == 2
    assert all(lines[i][2] == "postgres" for i in rejects)
    assert max(rejects) < min(allows)
    assert lines[0] == ["local", "all", "postgres", "peer"]


def test_roles_script() -> None:
    text = (COMPOSE_DIR / "postgres" / "initdb" / "10-roles.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert "ON_ERROR_STOP=1" in text
    assert re.findall(r"CREATE ROLE (\w+)", text) == ["refinery_owner", "refinery_app"]
    assert "CREATE DATABASE refinery_watch OWNER refinery_owner;" in text
    assert "PASSWORD :'owner_pw'" in text
    assert "PASSWORD :'app_pw'" in text
    assert "<<'SQL'" in text
    for word in ("SUPERUSER", "CREATEROLE", "CREATEDB", "BYPASSRLS"):
        assert word not in text
    assert not re.search(r"GRANT .* ON (ALL )?(TABLE|TABLES|SEQUENCES|SCHEMA)", text)
