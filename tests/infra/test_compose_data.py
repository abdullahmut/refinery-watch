"""Checks the data Droplet Compose files by parsing them. No Docker, no network."""

import re
from pathlib import Path
from typing import Any, Final

import hcl2
import pytest
import yaml

ROOT: Final[Path] = Path(__file__).resolve().parents[2]
COMPOSE_DIR: Final[Path] = ROOT / "infra" / "compose" / "data"
EXPECTED_VARS: Final[frozenset[str]] = frozenset(
    {"POSTGRES_PASSWORD", "PG_OWNER_PASSWORD", "PG_APP_PASSWORD", "DATA_BIND_ADDR"}
)
MEM_LIMITS_MIB: Final[dict[str, int]] = {"postgres": 384}
VOLUMES: Final[frozenset[str]] = frozenset({"pgdata"})
SERVICES: Final[frozenset[str]] = frozenset(MEM_LIMITS_MIB)
FILES: Final[tuple[str, ...]] = ("compose.yaml", "compose.vpc.yaml", "compose.local.yaml")
STACKS: Final[dict[str, tuple[str, ...]]] = {
    "production": ("compose.yaml", "compose.vpc.yaml"),
    "laptop": ("compose.yaml", "compose.local.yaml"),
}
OVERRIDE_KEYS: Final[frozenset[str]] = frozenset({"ports", "volumes"})
HBA_TARGET: Final[str] = "/etc/postgresql/pg_hba.conf"
BIND_HOST: Final[str] = "${DATA_BIND_ADDR:?set DATA_BIND_ADDR to the data Droplet private IP}"
PG_SETTINGS: Final[dict[str, str]] = {
    "shared_buffers": "96MB",
    "work_mem": "4MB",
    "maintenance_work_mem": "32MB",
    "autovacuum_max_workers": "2",
    "autovacuum_work_mem": "16MB",
    "max_connections": "15",
    "superuser_reserved_connections": "3",
    "hash_mem_multiplier": "1",
    "temp_buffers": "1MB",
    "max_parallel_workers_per_gather": "0",
    "effective_cache_size": "256MB",
    "timezone": "UTC",
    "log_timezone": "UTC",
    "hba_file": HBA_TARGET,
}
POSTGRES_IMAGE: Final[str] = (
    "postgres:16.15-bookworm@sha256:5264710be20f409750ac6dc4e5706dcef929c87e241605463f0f9aefa932a8d9"
)
ROLES_SQL: Final[tuple[str, ...]] = (
    "SET log_min_error_statement TO panic;",
    "\\set VERBOSITY terse",
    "\\getenv owner_pw PG_OWNER_PASSWORD",
    "\\getenv app_pw PG_APP_PASSWORD",
    "CREATE ROLE refinery_owner LOGIN PASSWORD :'owner_pw' CONNECTION LIMIT 3;",
    "CREATE ROLE refinery_app LOGIN PASSWORD :'app_pw' CONNECTION LIMIT 8;",
    "CREATE DATABASE refinery_watch OWNER refinery_owner;",
    "REVOKE ALL ON DATABASE refinery_watch FROM PUBLIC;",
    "GRANT CONNECT ON DATABASE refinery_watch TO refinery_app;",
    "REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;",
    "REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;",
)
TEST_ROLES: Final[str] = "refinery_test_owner,refinery_test_app"
HBA_RULES: Final[dict[str, list[tuple[str, ...]]]] = {
    "production": [
        ("local", "all", "postgres", "peer"),
        ("local", "all", "all", "scram-sha-256"),
        ("host", "all", "postgres", "0.0.0.0/0", "reject"),
        ("host", "all", "postgres", "::/0", "reject"),
        ("host", "all", TEST_ROLES, "0.0.0.0/0", "reject"),
        ("host", "all", TEST_ROLES, "::/0", "reject"),
        ("host", "all", "all", "10.110.0.0/24", "scram-sha-256"),
        ("host", "all", "all", "0.0.0.0/0", "reject"),
        ("host", "all", "all", "::/0", "reject"),
    ],
    "laptop": [
        ("local", "all", "postgres", "peer"),
        ("local", "all", "all", "scram-sha-256"),
        ("host", "all", "postgres", "0.0.0.0/0", "reject"),
        ("host", "all", "postgres", "::/0", "reject"),
        ("host", "all", "all", "0.0.0.0/0", "scram-sha-256"),
        ("host", "all", "all", "::/0", "scram-sha-256"),
    ],
}
BACKEND_OVERHEAD_MIB: Final[int] = 6
BACKGROUND_MIB: Final[int] = 30
TOTAL_LIMIT_MIB: Final[int] = 704
LOGGING: Final[dict[str, Any]] = {
    "driver": "json-file",
    "options": {"max-size": "10m", "max-file": "3"},
}
HEALTH_KEYS: Final[frozenset[str]] = frozenset(
    {"test", "interval", "timeout", "retries", "start_period"}
)
HEALTH_COMMAND: Final[str] = (
    "gosu postgres psql -h /var/run/postgresql -U postgres -d postgres -tAc "
    "\"select 1 from pg_roles where rolname='refinery_app' "
    "and current_setting('listen_addresses') <> ''\" | grep -q 1"
)
VAR_PATTERN: Final[re.Pattern[str]] = re.compile(r"\$\{([A-Za-z_]+)([^}]*)\}")
BARE_DOLLAR: Final[re.Pattern[str]] = re.compile(r"\$(?!\{)")


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


def port_strings(name: str) -> list[tuple[str, str]]:
    """Return (service, port) pairs for every published port in one file."""
    return [
        (service_name, str(port))
        for service_name, service in services(name).items()
        for port in service.get("ports", [])
    ]


def merged(stack: str) -> dict[str, dict[str, Any]]:
    """Return the services of a stack as Compose merges them.

    Overrides may only add ports and volumes. Volumes merge by container path.
    """
    base_file, *overrides = STACKS[stack]
    result = services(base_file)
    for name in overrides:
        assert set(load(name)) == {"services"}, name
        for service_name, extra in services(name).items():
            assert service_name in result, (name, service_name)
            assert set(extra) <= OVERRIDE_KEYS, (name, set(extra))
            target = result[service_name]
            target["ports"] = [*target.get("ports", []), *extra.get("ports", [])]
            by_path = {m.split(":")[1]: m for m in target.get("volumes", [])}
            by_path.update({m.split(":")[1]: m for m in extra.get("volumes", [])})
            target["volumes"] = list(by_path.values())
    return result


def mib(value: str) -> int:
    """Parse sizes such as 384m, 96MB and 192MiB into MiB."""
    match = re.fullmatch(r"(\d+)(m|mb|mib)", value.strip().lower())
    assert match, value
    return int(match.group(1))


def pg_settings(stack: str = "production") -> dict[str, str]:
    """Return the -c key=value pairs of the postgres command in a stack."""
    command = merged(stack)["postgres"]["command"]
    assert command[0] == "postgres"
    assert set(command[1::2]) == {"-c"}
    found: dict[str, str] = {}
    for item in command[2::2]:
        key, _, value = item.partition("=")
        found[key] = value
    return found


def hba_rules(stack: str) -> list[tuple[str, ...]]:
    """Return the rules of the pg_hba.conf file a stack mounts."""
    mounts = [m for m in merged(stack)["postgres"]["volumes"] if m.split(":")[1] == HBA_TARGET]
    assert len(mounts) == 1
    path = (COMPOSE_DIR / mounts[0].split(":")[0]).resolve()
    return [
        tuple(line.split())
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def makefile_recipe(target: str) -> list[str]:
    """Return the recipe lines of one Makefile target, with variables expanded."""
    lines = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    variables = {
        m.group(1): m.group(2) for line in lines if (m := re.match(r"(\w+) := (.*)", line))
    }
    start = lines.index(f"{target}:")
    recipe: list[str] = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        text = line.strip()
        for key, value in variables.items():
            text = text.replace(f"$({key})", value)
        recipe.append(text)
    return recipe


def test_services_volumes_and_name() -> None:
    data = load("compose.yaml")
    assert data["name"] == "refinery-watch-data"
    assert "version" not in data
    assert set(data["services"]) == SERVICES
    assert set(data["volumes"]) == VOLUMES
    assert all(not options for options in data["volumes"].values())


def test_overrides_only_add_ports_and_volumes() -> None:
    for stack in STACKS:
        assert set(merged(stack)) == SERVICES


@pytest.mark.parametrize("stack", STACKS)
def test_restart_policies(stack: str) -> None:
    for name, service in merged(stack).items():
        assert service["restart"] == "always", name


@pytest.mark.parametrize("stack", STACKS)
def test_memory_limits(stack: str) -> None:
    found = merged(stack)
    for name, limit in MEM_LIMITS_MIB.items():
        assert found[name]["mem_limit"] == f"{limit}m"
        assert found[name]["memswap_limit"] == found[name]["mem_limit"]
    assert sum(mib(s["mem_limit"]) for s in found.values()) <= TOTAL_LIMIT_MIB


@pytest.mark.parametrize("stack", STACKS)
def test_healthchecks(stack: str) -> None:
    check = merged(stack)["postgres"]["healthcheck"]
    assert set(check) == HEALTH_KEYS
    assert check["test"] == ["CMD-SHELL", HEALTH_COMMAND]
    assert check["start_period"] == "60s"


@pytest.mark.parametrize("stack", STACKS)
def test_postgres_settings_fit_limit(stack: str) -> None:
    settings = pg_settings(stack)
    assert settings == PG_SETTINGS
    limit = mib(merged(stack)["postgres"]["mem_limit"])
    assert mib(settings["shared_buffers"]) * 4 <= limit
    worst = (
        mib(settings["shared_buffers"])
        + int(settings["max_connections"])
        * (
            mib(settings["work_mem"]) * int(settings["hash_mem_multiplier"])
            + mib(settings["temp_buffers"])
            + BACKEND_OVERHEAD_MIB
        )
        + int(settings["autovacuum_max_workers"]) * mib(settings["autovacuum_work_mem"])
        + mib(settings["maintenance_work_mem"])
        + BACKGROUND_MIB
    )
    assert worst < limit


def test_stop_grace_period_avoids_sigkill() -> None:
    assert services("compose.yaml")["postgres"]["stop_grace_period"] == "60s"


def test_role_connection_limits_fit_max_connections() -> None:
    limits = [int(n) for n in re.findall(r"CONNECTION LIMIT (\d+)", " ".join(ROLES_SQL))]
    settings = pg_settings()
    usable = int(settings["max_connections"]) - int(settings["superuser_reserved_connections"])
    assert len(limits) == 2
    assert sum(limits) <= usable


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


@pytest.mark.parametrize("stack", STACKS)
def test_logging_and_no_new_privileges(stack: str) -> None:
    for service in merged(stack).values():
        assert service["logging"] == LOGGING
        assert service["security_opt"] == ["no-new-privileges:true"]


def test_base_file_publishes_nothing() -> None:
    for service in services("compose.yaml").values():
        assert "ports" not in service
        assert "expose" not in service


def test_interpolated_vars_are_known_and_required() -> None:
    for name in FILES:
        text = (COMPOSE_DIR / name).read_text(encoding="utf-8")
        assert not BARE_DOLLAR.search(text), name
        for var, tail in VAR_PATTERN.findall(text):
            assert var in EXPECTED_VARS, var
            assert tail.startswith(":?"), (var, tail)


@pytest.mark.parametrize("stack", STACKS)
def test_images_pinned_by_digest(stack: str) -> None:
    for name, service in merged(stack).items():
        assert re.fullmatch(r"postgres:16\.\d+-bookworm@sha256:[0-9a-f]{64}", service["image"]), (
            name
        )
    assert merged(stack)["postgres"]["image"] == POSTGRES_IMAGE


@pytest.mark.parametrize("stack", STACKS)
def test_pg_hba_rules(stack: str) -> None:
    assert hba_rules(stack) == HBA_RULES[stack]


def test_roles_script() -> None:
    text = (COMPOSE_DIR / "postgres" / "initdb" / "10-roles.sh").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "#!/usr/bin/env bash"
    assert "set -euo pipefail" in lines
    assert not [line for line in lines if re.match(r"\s*set .*-[a-z]*[xv]", line)]
    command = next(line for line in lines if line.startswith("psql "))
    assert "_pw" not in command and "PASSWORD" not in command
    assert "--username postgres" in command
    body = text.split("<<'SQL'\n", 1)[1].split("\nSQL", 1)[0].splitlines()
    assert tuple(body) == ROLES_SQL
    assert not re.search(r"PASSWORD\s+'", text)


def test_vpc_ports_need_bind_address() -> None:
    assert port_strings("compose.vpc.yaml") == [("postgres", f"{BIND_HOST}:5432:5432")]


def test_no_all_interfaces_bind() -> None:
    for name in FILES:
        for _, port in port_strings(name):
            assert "0.0.0.0" not in port  # noqa: S104
            assert "::" not in port.replace(BIND_HOST, "")
            assert len(port.replace(BIND_HOST, "HOST").split(":")) >= 3, port


def test_local_ports_on_loopback() -> None:
    assert port_strings("compose.local.yaml") == [("postgres", "127.0.0.1:5432:5432")]


def test_test_roles_only_local() -> None:
    mount = "20-test-roles.sql"
    for stack, files in STACKS.items():
        mounted = any(mount in volume for volume in merged(stack)["postgres"]["volumes"])
        assert mounted == (files[-1] == "compose.local.yaml"), stack
    for name in FILES:
        mounted = any(
            mount in volume
            for service in services(name).values()
            for volume in service.get("volumes", [])
        )
        assert mounted == (name == "compose.local.yaml"), name
    text = (COMPOSE_DIR / "postgres" / "initdb-local" / mount).read_text(encoding="utf-8")
    assert re.findall(r"CREATE ROLE (\w+)", text) == ["refinery_test_owner", "refinery_test_app"]
    assert re.findall(r"CREATE DATABASE (\w+) OWNER (\w+)", text) == [
        ("refinery_watch_test", "refinery_test_owner")
    ]
    assert "SUPERUSER" not in text


def test_make_up_and_down() -> None:
    up = makefile_recipe("up")
    down = makefile_recipe("down")
    assert up[0].startswith("[ -f .env ] ||")
    assert up[1].startswith("docker info ")
    assert up[2].startswith("docker compose ")
    assert len(up) == 3
    assert (
        'echo "up: .env not found at the repo root. '
        'Copy .env.example to .env and fill it in." >&2' in up[0]
    )
    assert (
        'echo "up: Docker is not running. Start Docker Desktop, then run make up again." >&2'
        in up[1]
    )
    assert "up -d --wait --wait-timeout 180" in up[2]
    assert up[2].startswith("docker compose -p refinery-watch-local ")
    assert down[1].startswith("docker compose -p refinery-watch-local ")
    assert down[0].startswith("docker info ")
    assert 'echo "down: Docker is not running. Nothing to stop." >&2' in down[0]
    assert down[1].endswith("compose.local.yaml down")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert not re.search(r"(^|\s)(-v|--volumes)(\s|$)", " ".join(down))
    assert "compose.vpc.yaml" not in makefile
    assert "down" in makefile.split(".PHONY:")[1].splitlines()[0].split()
    assert "$(shell" not in makefile


def test_hba_allows_only_the_terraform_vpc_range() -> None:
    parsed = hcl2.loads((ROOT / "infra" / "terraform" / "variables.tf").read_text(encoding="utf-8"))
    (default,) = (
        body["default"]
        for block in parsed["variable"]
        for name, body in block.items()
        if name.strip('"') == "vpc_ip_range"
    )
    allowed = {
        rule[3] for rule in hba_rules("production") if rule[0] == "host" and rule[4] != "reject"
    }
    assert allowed == {default.strip('"')}


def test_test_app_role_matches_production_app_role() -> None:
    path = COMPOSE_DIR / "postgres" / "initdb-local" / "20-test-roles.sql"
    statements = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("--")
    ]
    expected = [
        line.replace("refinery_app", "refinery_test_app")
        .replace("refinery_owner", "refinery_test_owner")
        .replace("refinery_watch", "refinery_watch_test")
        for line in ROLES_SQL
        if line.startswith(("CREATE ", "REVOKE ALL", "GRANT "))
    ]
    password = re.compile(r"PASSWORD (:'\w+'|'\w+')")
    assert [password.sub("PASSWORD x", s) for s in statements] == [
        password.sub("PASSWORD x", s) for s in expected
    ]
