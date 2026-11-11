"""Checks the Terraform config by parsing the .tf files. No network, no terraform binary."""

import re
from pathlib import Path
from typing import Any, Final

import hcl2

TF_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "infra" / "terraform"
CLOUD_INIT_DIR: Final[Path] = TF_DIR.parent / "cloud-init"
FIREWALLS: Final[frozenset[str]] = frozenset({"base", "data", "app", "ingest"})
ROLES: Final[tuple[str, ...]] = ("data", "app", "ingest")
SSH_PORT_EXPR: Final[str] = "tostring(var.ssh_port)"
COMMENTS_KEY: Final[str] = "__comments__"
INTERNET: Final[list[str]] = ["0.0.0.0/0", "::/0"]
# (protocol, port_range, destinations) for each role's egress.
EGRESS: Final[set[tuple[str, str, tuple[str, ...]]]] = {
    ("tcp", "80", tuple(INTERNET)),
    ("tcp", "443", tuple(INTERNET)),
    ("tcp", "53", tuple(INTERNET)),
    ("udp", "53", tuple(INTERNET)),
    ("udp", "123", tuple(INTERNET)),
    ("tcp", "1-65535", ("var.vpc_ip_range",)),
    ("udp", "1-65535", ("var.vpc_ip_range",)),
}
BLOCK_MARKER: Final[str] = "__is_block__"


def unwrap(value: Any) -> Any:
    """Strips the quotes and the ${} wrapper that python-hcl2 keeps around values."""
    if isinstance(value, str):
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            return value[1:-1]
        if value.startswith("${") and value.endswith("}"):
            return value[2:-1]
        return value
    if isinstance(value, list):
        return [unwrap(v) for v in value]
    if isinstance(value, dict):
        return {unwrap(k): unwrap(v) for k, v in value.items() if k != BLOCK_MARKER}
    return value


def load_tf() -> dict[str, list[Any]]:
    """Merges the blocks of every .tf file, keyed by block type."""
    merged: dict[str, list[Any]] = {}
    for path in sorted(TF_DIR.glob("*.tf")):
        parsed = hcl2.loads(path.read_text(encoding="utf-8"))
        for block_type, blocks in parsed.items():
            if block_type == COMMENTS_KEY:
                continue
            merged.setdefault(block_type, []).extend(blocks)
    return merged


def resources(kind: str) -> dict[str, dict[str, Any]]:
    """Resources of one type, keyed by name, with values unwrapped."""
    found: dict[str, dict[str, Any]] = {}
    for block in load_tf()["resource"]:
        for res_type, named in block.items():
            if unwrap(res_type) == kind:
                for name, body in named.items():
                    found[unwrap(name)] = unwrap(body)
    return found


def variables() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for block in load_tf()["variable"]:
        for name, body in block.items():
            found[unwrap(name)] = unwrap(body)
    return found


def local_values() -> dict[str, Any]:
    found: dict[str, Any] = {}
    for block in load_tf()["locals"]:
        found.update(unwrap(block))
    return found


def ssh_port_default() -> int:
    port = variables()["ssh_port"]["default"]
    assert isinstance(port, int)
    return port


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def ports_of(rule: dict[str, Any]) -> set[int]:
    """Ports a rule opens. icmp opens none. tcp and udp rules must set port_range."""
    if rule["protocol"] == "icmp":
        return set()
    spec = rule.get("port_range")
    assert spec, "tcp and udp rules need a port_range"
    if spec == SSH_PORT_EXPR:
        return {ssh_port_default()}
    low, _, high = spec.partition("-")
    return set(range(int(low), int(high or low) + 1))


def inbound_rules() -> dict[str, list[dict[str, Any]]]:
    """Inbound rules per firewall resource name."""
    return {
        name: list(body.get("inbound_rule", []))
        for name, body in resources("digitalocean_firewall").items()
    }


def open_ports(firewall: str) -> set[int]:
    result: set[int] = set()
    for rule in inbound_rules()[firewall]:
        assert rule["protocol"] == "tcp"
        result |= ports_of(rule)
    return result


def variable_regex(variable: str) -> str:
    """The regex literal that a validation of `variable` passes to regex()."""
    for validation in variables()[variable]["validation"]:
        match = re.search(r'regex\("(.+?)", var\.' + variable + r"\)", validation["condition"])
        if match:
            return match.group(1)
    raise AssertionError(f"no regex validation on {variable}")


def matches(pattern: str, value: str) -> bool:
    """Whole-string match, like RE2 without the multiline flag."""
    return re.match(pattern.replace("$", r"\Z"), value) is not None


def test_provider_version_is_exact() -> None:
    terraform = unwrap(load_tf()["terraform"][0])
    assert terraform["required_version"]
    provider = terraform["required_providers"][0]["digitalocean"]
    assert provider["source"] == "digitalocean/digitalocean"
    assert provider["version"].startswith("=")
    assert not provider["version"].startswith("==")


def test_no_backend_or_cloud_block() -> None:
    for block in load_tf()["terraform"]:
        assert "backend" not in block
        assert "cloud" not in block


def test_no_token_in_config() -> None:
    provider = unwrap(load_tf()["provider"][0])
    assert list(provider) == ["digitalocean"]
    assert provider["digitalocean"] == {}
    assert not [name for name in variables() if "token" in name.lower()]


def test_ssh_variables() -> None:
    declared = variables()
    port = declared["ssh_port"]
    assert port["default"] != 22
    assert port["validation"]
    assert "default" not in declared["ssh_source_addresses"]
    assert declared["ssh_source_addresses"]["validation"]


def test_port_22_never_open() -> None:
    for firewall in inbound_rules():
        assert 22 not in open_ports(firewall)


def test_data_ports_only_from_app_and_ingest_tags_on_data() -> None:
    expected = ['digitalocean_tag.role["app"].id', 'digitalocean_tag.role["ingest"].id']
    for firewall, rule_list in inbound_rules().items():
        for rule in rule_list:
            if ports_of(rule) & {5432, 9000}:
                assert firewall == "data"
                assert rule["source_tags"] == expected
                assert "source_addresses" not in rule
    assert open_ports("data") == {5432, 9000}


def test_https_only_on_app() -> None:
    for firewall in inbound_rules():
        assert (443 in open_ports(firewall)) == (firewall == "app")
    assert open_ports("app") == {443}


def test_no_other_inbound_ports() -> None:
    every: set[int] = set()
    for firewall in inbound_rules():
        every |= open_ports(firewall)
    assert every == {ssh_port_default(), 443, 5432, 9000}


def test_ssh_rule_port_is_the_variable() -> None:
    (rule,) = inbound_rules()["base"]
    assert rule["port_range"] == SSH_PORT_EXPR
    assert rule["protocol"] == "tcp"
    assert rule["source_addresses"] == "var.ssh_source_addresses"


def test_role_egress_is_exactly_the_allowed_set() -> None:
    firewalls = resources("digitalocean_firewall")
    assert "outbound_rule" not in firewalls["base"]
    for role in ROLES:
        rules = firewalls[role]["outbound_rule"]
        got = {
            (r["protocol"], r["port_range"], tuple(_as_list(r["destination_addresses"])))
            for r in rules
        }
        assert got == EGRESS, role
        assert len(rules) == len(EGRESS), role


def test_firewalls_attach_by_tag() -> None:
    firewalls = resources("digitalocean_firewall")
    assert set(firewalls) == FIREWALLS
    for body in firewalls.values():
        assert body["tags"]
        assert "droplet_ids" not in body


def test_droplet_region_image_vpc_sizes() -> None:
    node = resources("digitalocean_droplet")["node"]
    locals_ = local_values()
    assert locals_["region"] == "lon1"
    assert locals_["image"] == "ubuntu-24-04-x64"
    assert node["region"] == "local.region"
    assert node["image"] == "local.image"
    assert node["vpc_uuid"] == "digitalocean_vpc.main.id"
    assert variables()["droplet_sizes"]["default"] == {
        "data": "s-1vcpu-1gb",
        "app": "s-1vcpu-1gb",
        "ingest": "s-1vcpu-512mb-10gb",
    }


def test_droplets_wait_for_firewalls_and_ignore_user_data() -> None:
    node = resources("digitalocean_droplet")["node"]
    assert set(node["depends_on"]) == {f"digitalocean_firewall.{name}" for name in FIREWALLS}
    assert len(node["depends_on"]) == len(FIREWALLS)
    ignored = node["lifecycle"][0]["ignore_changes"]
    assert {"user_data", "ssh_keys"} <= set(ignored)


def test_droplets_carry_firewall_tags() -> None:
    node = resources("digitalocean_droplet")["node"]
    assert node["tags"] == ["digitalocean_tag.all.id", "digitalocean_tag.role[each.key].id"]
    tags = resources("digitalocean_tag")
    assert set(tags) == {"all", "role"}
    assert tags["role"]["for_each"] == "toset(local.roles)"
    firewalls = resources("digitalocean_firewall")
    assert firewalls["base"]["tags"] == ["digitalocean_tag.all.id"]
    for role in ROLES:
        assert firewalls[role]["tags"] == [f'digitalocean_tag.role["{role}"].id']


def test_outputs_present() -> None:
    names = {unwrap(name) for block in load_tf()["output"] for name in block}
    assert {"public_ipv4", "private_ipv4"} <= names


def test_every_role_has_cloud_init() -> None:
    roles = set(local_values()["roles"])
    templates = {p.name.removesuffix(".yaml.tftpl") for p in CLOUD_INIT_DIR.glob("*.yaml.tftpl")}
    assert roles == templates


def test_lock_file_matches_pinned_version() -> None:
    terraform = unwrap(load_tf()["terraform"][0])
    pinned = terraform["required_providers"][0]["digitalocean"]["version"].removeprefix("=").strip()
    lock = hcl2.loads((TF_DIR / ".terraform.lock.hcl").read_text(encoding="utf-8"))
    (provider,) = lock["provider"]
    ((name, body),) = provider.items()
    assert unwrap(name) == "registry.terraform.io/digitalocean/digitalocean"
    assert unwrap(body["version"]) == pinned
    assert any(unwrap(h).startswith("h1:") for h in body["hashes"])


def condition_of(variable: str, needle: str) -> str:
    for validation in variables()[variable]["validation"]:
        if needle in validation["condition"]:
            return str(validation["condition"])
    raise AssertionError(f"no validation on {variable} mentions {needle}")


def test_repo_url_validation_rejects_yaml_breaking_values() -> None:
    pattern = variable_regex("repo_url")
    assert matches(pattern, "https://github.com/owner/repo.git")
    assert matches(pattern, "https://github.com/Owner-1/re.po_x-y.git")
    bad = [
        'https://github.com/o/r".git',
        "https://github.com/o/r'.git",
        "https://github.com/o/r" + chr(92) + "x.git",
        "https://github.com/o/a,b.git",
        "https://github.com/o/[x].git",
        "https://github.com/o/{x}.git",
        "https://github.com/o/r.git\n",
        "https://github.com/o/r .git",
        "https://github.com/o/r#x.git",
        "https://user:tok@github.com/o/r.git",
        "https://github.com@evil.com/o/r.git",
        "https://evil.com/o/r.git",
        "http://github.com/o/r.git",
        "https://github.com/o/r",
    ]
    for value in bad:
        assert not matches(pattern, value), value


def test_ssh_public_key_validation() -> None:
    pattern = variable_regex("ssh_public_key")
    assert matches(pattern, "ssh-ed25519 AAAAC3Nza operator@laptop")
    assert matches(pattern, "ssh-rsa AAAAB3Nza==")
    assert matches(pattern, "sk-ssh-ed25519@openssh.com AAAAGnNr operator@laptop")
    for value in [
        "ssh-ed25519 AAAA\nssh-rsa BBBB",
        'ssh-ed25519 AAAA "x"',
        "ssh-dss AAAA x",
        "sk-ssh-ed25519@opensshXcom AAAA x",
    ]:
        assert not matches(pattern, value), value


def test_ssh_source_validation_blocks_anywhere_and_host_bits() -> None:
    declared = variables()
    flag = declared["allow_ssh_from_anywhere"]
    assert flag["type"] == "bool"
    assert flag["default"] is False
    anywhere = condition_of("ssh_source_addresses", "allow_ssh_from_anywhere")
    assert '"0.0.0.0/0"' in anywhere
    assert '"::/0"' in anywhere
    host_bits = condition_of("ssh_source_addresses", "cidrhost")
    assert 'split("/", a)[0]' in host_bits
    assert "length(var.ssh_source_addresses) > 0" in host_bits


def test_vpc_ip_range_is_private_ipv4_between_16_and_24() -> None:
    condition = condition_of("vpc_ip_range", "cidrhost")
    assert ">= 16" in condition
    assert "<= 24" in condition
    assert 'split("/", var.vpc_ip_range)[0]' in condition
    pattern = re.search(r'regex\("(.+?)", var\.vpc_ip_range\)', condition)
    assert pattern
    private = pattern.group(1)
    for good in ["10.110.0.0/24", "172.16.0.0/16", "172.31.4.0/24", "192.168.1.0/24"]:
        assert matches(private, good), good
    for bad in ["8.8.8.0/24", "172.32.0.0/16", "172.15.0.0/16", "11.0.0.0/24", "fd00::/64"]:
        assert not matches(private, bad), bad
    default = variables()["vpc_ip_range"]["default"]
    assert matches(private, default)


RUNBOOK: Final[Path] = TF_DIR.parents[1] / "docs" / "runbook.md"


def runbook_paragraphs() -> list[str]:
    text = RUNBOOK.read_text(encoding="utf-8").replace("\r\n", "\n")
    return [" ".join(p.split()) for p in text.split("\n\n")]


def vars_in(text: str) -> set[str]:
    return set(re.findall(r"\bvar\.([a-z_]+)", text))


def test_runbook_warns_about_variables_in_firewall_and_ignored_user_data() -> None:
    firewall_text = (TF_DIR / "firewall.tf").read_text(encoding="utf-8")
    user_data = resources("digitalocean_droplet")["node"]["user_data"]
    shared = vars_in(firewall_text) & vars_in(user_data)
    assert shared, "expected at least ssh_port"
    paragraphs = runbook_paragraphs()
    for name in shared:
        assert any(f"`{name}`" in p and "`-replace`" in p for p in paragraphs), name


def test_runbook_warns_that_vpc_range_change_replaces_droplets() -> None:
    assert any(
        "`vpc_ip_range`" in p and "replaces all three Droplets" in p for p in runbook_paragraphs()
    )


def test_runbook_imports_ssh_key_by_numeric_id() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "doctl compute ssh-key list" in text
    assert "terraform import digitalocean_ssh_key.operator <numeric key id>" in text
    assert "<fingerprint>" not in text


def test_runbook_looks_up_ipv4_address_with_curl_4() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "curl -4 -s https://ifconfig.me" in text
