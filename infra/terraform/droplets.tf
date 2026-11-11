resource "digitalocean_droplet" "node" {
  for_each = toset(local.roles)

  name       = "${local.name_prefix}-${each.key}"
  region     = local.region
  image      = local.image
  size       = var.droplet_sizes[each.key]
  vpc_uuid   = digitalocean_vpc.main.id
  ssh_keys   = [digitalocean_ssh_key.operator.fingerprint]
  tags       = [digitalocean_tag.all.id, digitalocean_tag.role[each.key].id]
  ipv6       = false
  monitoring = false
  backups    = false

  user_data = templatefile("${path.module}/../cloud-init/${each.key}.yaml.tftpl", {
    ssh_port       = var.ssh_port
    ssh_public_key = var.ssh_public_key
    repo_url       = var.repo_url
    repo_ref       = var.repo_ref
  })

  depends_on = [
    digitalocean_firewall.base,
    digitalocean_firewall.data,
    digitalocean_firewall.app,
  ]

  lifecycle {
    ignore_changes = [user_data, ssh_keys]
  }
}
