# A Droplet gets the combined rules of its firewalls. With no outbound rule from any of them,
# egress is blocked. Each role firewall allows web, DNS and NTP to the internet and anything
# inside the VPC.
locals {
  internet = ["0.0.0.0/0", "::/0"]
  role_egress = [
    { protocol = "tcp", port_range = "80", destination_addresses = local.internet },
    { protocol = "tcp", port_range = "443", destination_addresses = local.internet },
    { protocol = "tcp", port_range = "53", destination_addresses = local.internet },
    { protocol = "udp", port_range = "53", destination_addresses = local.internet },
    { protocol = "udp", port_range = "123", destination_addresses = local.internet },
    { protocol = "tcp", port_range = "1-65535", destination_addresses = [var.vpc_ip_range] },
    { protocol = "udp", port_range = "1-65535", destination_addresses = [var.vpc_ip_range] },
  ]
}

resource "digitalocean_firewall" "base" {
  name = "${local.name_prefix}-base"
  tags = [digitalocean_tag.all.id]

  inbound_rule {
    protocol         = "tcp"
    port_range       = tostring(var.ssh_port)
    source_addresses = var.ssh_source_addresses
  }
}

resource "digitalocean_firewall" "data" {
  name = "${local.name_prefix}-data"
  tags = [digitalocean_tag.role["data"].id]

  inbound_rule {
    protocol    = "tcp"
    port_range  = "5432"
    source_tags = [digitalocean_tag.role["app"].id, digitalocean_tag.role["ingest"].id]
  }

  inbound_rule {
    protocol    = "tcp"
    port_range  = "9000"
    source_tags = [digitalocean_tag.role["app"].id, digitalocean_tag.role["ingest"].id]
  }

  dynamic "outbound_rule" {
    for_each = local.role_egress
    content {
      protocol              = outbound_rule.value.protocol
      port_range            = outbound_rule.value.port_range
      destination_addresses = outbound_rule.value.destination_addresses
    }
  }
}

resource "digitalocean_firewall" "app" {
  name = "${local.name_prefix}-app"
  tags = [digitalocean_tag.role["app"].id]

  inbound_rule {
    protocol         = "tcp"
    port_range       = "443"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  dynamic "outbound_rule" {
    for_each = local.role_egress
    content {
      protocol              = outbound_rule.value.protocol
      port_range            = outbound_rule.value.port_range
      destination_addresses = outbound_rule.value.destination_addresses
    }
  }
}

resource "digitalocean_firewall" "ingest" {
  name = "${local.name_prefix}-ingest"
  tags = [digitalocean_tag.role["ingest"].id]

  dynamic "outbound_rule" {
    for_each = local.role_egress
    content {
      protocol              = outbound_rule.value.protocol
      port_range            = outbound_rule.value.port_range
      destination_addresses = outbound_rule.value.destination_addresses
    }
  }
}
