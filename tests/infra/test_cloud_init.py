"""Checks the cloud-init templates by rendering them with sample values. No network."""

import re
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

CLOUD_INIT_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "infra" / "cloud-init"
ROLES: Final[tuple[str, ...]] = ("data", "app", "ingest")
TEMPLATE_VARS: Final[frozenset[str]] = frozenset(
    {"ssh_port", "ssh_public_key", "repo_url", "repo_ref"}
)
SAMPLE: Final[dict[str, str]] = {
    "ssh_port": "2345",
    "ssh_public_key": "ssh-ed25519 AAAASAMPLEKEY sample@host",
    "repo_url": "https://example.com/owner/repo.git",
    "repo_ref": "v9.9.9",
}
PACKAGES: Final[frozenset[str]] = frozenset(
    {
        "docker.io",
        "docker-compose-v2",
        "fail2ban",
        "python3-systemd",
        "unattended-upgrades",
        "git",
    }
)
INTERPOLATION: Final[re.Pattern[str]] = re.compile(r"(?<!\$)\$\{([^}]*)\}")


def read_template(role: str) -> str:
    return (CLOUD_INIT_DIR / f"{role}.yaml.tftpl").read_text(encoding="utf-8")


def render_text(role: str) -> str:
    text = INTERPOLATION.sub(lambda m: SAMPLE[m.group(1)], read_template(role))
    return text.replace("$${", "${")


def render(role: str) -> dict[str, Any]:
    loaded = yaml.safe_load(render_text(role))
    assert isinstance(loaded, dict)
    return loaded


def written_file(role: str, path: str) -> str:
    for entry in render(role)["write_files"]:
        if entry["path"] == path:
            assert entry["permissions"] == "0644"
            return str(entry["content"])
    raise AssertionError(f"{path} not written")


def runcmd_index(role: str, word: str) -> int:
    """Index of the first runcmd entry that holds `word` as an item or inside an item."""
    for i, cmd in enumerate(render(role)["runcmd"]):
        parts = cmd if isinstance(cmd, list) else [cmd]
        if any(word in part for part in parts):
            return i
    raise AssertionError(f"{word} not in runcmd")


@pytest.mark.parametrize("role", ROLES)
def test_template_vars_are_exactly_the_four(role: str) -> None:
    text = read_template(role)
    assert "%{" not in text
    assert set(INTERPOLATION.findall(text)) == TEMPLATE_VARS


@pytest.mark.parametrize("role", ROLES)
def test_first_line_is_cloud_config(role: str) -> None:
    assert read_template(role).splitlines()[0] == "#cloud-config"


@pytest.mark.parametrize("role", ROLES)
def test_password_and_root_login_off(role: str) -> None:
    config = render(role)
    assert config["ssh_pwauth"] is False
    assert config["disable_root"] is True


@pytest.mark.parametrize("role", ROLES)
def test_deploy_user_is_key_only(role: str) -> None:
    users = render(role)["users"]
    assert [u["name"] for u in users] == ["deploy"]
    user = users[0]
    assert user["lock_passwd"] is True
    assert user["ssh_authorized_keys"] == [SAMPLE["ssh_public_key"]]
    assert "passwd" not in user
    assert "hashed_passwd" not in user


@pytest.mark.parametrize("role", ROLES)
def test_required_packages(role: str) -> None:
    config = render(role)
    assert set(config["packages"]) >= PACKAGES
    assert config["package_update"] is True
    assert config["package_upgrade"] is True


@pytest.mark.parametrize("role", ROLES)
def test_sshd_dropin_sets_port_and_sorts_first(role: str) -> None:
    path = "/etc/ssh/sshd_config.d/10-refinery-watch.conf"
    lines = written_file(role, path).splitlines()
    assert Path(path).name < "50-cloud-init.conf"
    assert lines == [
        "Port 2345",
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "PermitRootLogin no",
        "PubkeyAuthentication yes",
        "AllowUsers deploy",
    ]


@pytest.mark.parametrize("role", ROLES)
def test_fail2ban_jail_watches_ssh_port(role: str) -> None:
    lines = written_file(role, "/etc/fail2ban/jail.d/sshd.local").splitlines()
    assert lines[0] == "[sshd]"
    assert set(lines[1:]) == {
        "enabled = true",
        "port = 2345",
        "backend = systemd",
        "maxretry = 5",
        "findtime = 10m",
        "bantime = 1h",
    }


@pytest.mark.parametrize("role", ROLES)
def test_unattended_upgrades_enabled(role: str) -> None:
    lines = written_file(role, "/etc/apt/apt.conf.d/20auto-upgrades").splitlines()
    assert 'APT::Periodic::Update-Package-Lists "1";' in lines
    assert 'APT::Periodic::Unattended-Upgrade "1";' in lines


@pytest.mark.parametrize("role", ROLES)
def test_sshd_restart_runs_before_clone(role: str) -> None:
    runcmd = render(role)["runcmd"]
    assert runcmd[0] == ["sshd", "-t"]
    assert runcmd[1] == ["systemctl", "daemon-reload"]
    restart = runcmd_index(role, "ssh.socket")
    assert restart == runcmd_index(role, "ssh.service")
    assert 1 < restart < runcmd_index(role, "clone")


@pytest.mark.parametrize("role", ROLES)
def test_clone_is_https_with_ref_and_no_credentials(role: str) -> None:
    clone = next(c for c in render(role)["runcmd"] if isinstance(c, list) and "clone" in c)
    assert clone[:4] == ["sudo", "-u", "deploy", "git"]
    assert clone[clone.index("--branch") + 1] == SAMPLE["repo_ref"]
    url = clone[-2]
    assert url == SAMPLE["repo_url"]
    assert url.startswith("https://")
    assert "@" not in url


@pytest.mark.parametrize("role", ROLES)
def test_no_secret_words_in_rendered_text(role: str) -> None:
    text = render_text(role).lower().replace("passwordauthentication", "")
    for word in ("token", "password", "secret"):
        assert word not in text
