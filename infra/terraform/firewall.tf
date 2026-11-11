resource "digitalocean_firewall" "base" {
  name = "${local.name_prefix}-base"
  tags = [digitalocean_tag.all.id]

  inbound_rule {
    protocol         = "tcp"
    port_range       = tostring(var.ssh_port)
    source_addresses = var.ssh_source_addresses
  }

  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "icmp"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}

resource "digitalocean_firewall" "data" {
  name = "${local.name_prefix}-data"
  tags = [digitalocean_tag.role["data"].id]

  inbound_rule {
    protocol         = "tcp"
    port_range       = "5432"
    source_addresses = [var.vpc_ip_range]
  }

  inbound_rule {
    protocol         = "tcp"
    port_range       = "9000"
    source_addresses = [var.vpc_ip_range]
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
}
