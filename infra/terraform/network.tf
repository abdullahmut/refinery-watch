locals {
  region      = "lon1"
  image       = "ubuntu-24-04-x64"
  roles       = ["data", "app", "ingest"]
  name_prefix = "refinery-watch"
}

resource "digitalocean_vpc" "main" {
  name     = "${local.name_prefix}-lon1"
  region   = local.region
  ip_range = var.vpc_ip_range
}

resource "digitalocean_ssh_key" "operator" {
  name       = "${local.name_prefix}-operator"
  public_key = var.ssh_public_key
}

resource "digitalocean_tag" "all" {
  name = local.name_prefix
}

resource "digitalocean_tag" "role" {
  for_each = toset(local.roles)
  name     = "${local.name_prefix}-${each.key}"
}
