output "public_ipv4" {
  value = { for k, d in digitalocean_droplet.node : k => d.ipv4_address }
}

output "private_ipv4" {
  value = { for k, d in digitalocean_droplet.node : k => d.ipv4_address_private }
}

output "ssh_port" {
  value = var.ssh_port
}

output "vpc_ip_range" {
  value = digitalocean_vpc.main.ip_range
}
