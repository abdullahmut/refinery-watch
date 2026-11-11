"""Checks the Terraform config by parsing the .tf files. No network, no terraform binary."""

from pathlib import Path
from typing import Any, Final

import hcl2

TF_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "infra" / "terraform"
CLOUD_INIT_DIR: Final[Path] = TF_DIR.parent / "cloud-init"
FIREWALLS: Final[frozenset[str]] = frozenset({"base", "data", "app"})
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
            if block_type == "__comments__":
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


def ports_of(rule: dict[str, Any]) -> set[int]:
    """Ports a rule opens. A missing port range on tcp or udp means all ports."""
    if rule["protocol"] == "icmp":
        return set()
    spec = rule.get("port_range")
    assert spec, "tcp and udp rules need a port_range"
    if spec == "tostring(var.ssh_port)":
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


def test_data_ports_only_from_vpc_on_data() -> None:
    rules = inbound_rules()
    for firewall, rule_list in rules.items():
        for rule in rule_list:
            if ports_of(rule) & {5432, 9000}:
                assert firewall == "data"
                assert rule["source_addresses"] == ["var.vpc_ip_range"]
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


def test_ssh_rule_uses_allow_list() -> None:
    (rule,) = inbound_rules()["base"]
    assert rule["source_addresses"] == "var.ssh_source_addresses"


def test_base_firewall_allows_egress() -> None:
    outbound = resources("digitalocean_firewall")["base"]["outbound_rule"]
    assert {rule["protocol"] for rule in outbound} == {"tcp", "udp", "icmp"}
    for rule in outbound:
        assert rule["destination_addresses"] == ["0.0.0.0/0", "::/0"]


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
    ignored = node["lifecycle"][0]["ignore_changes"]
    assert {"user_data", "ssh_keys"} <= set(ignored)


def test_outputs_present() -> None:
    names = {unwrap(name) for block in load_tf()["output"] for name in block}
    assert {"public_ipv4", "private_ipv4"} <= names


def test_every_role_has_cloud_init() -> None:
    roles = set(local_values()["roles"])
    templates = {p.name.removesuffix(".yaml.tftpl") for p in CLOUD_INIT_DIR.glob("*.yaml.tftpl")}
    assert roles == templates
