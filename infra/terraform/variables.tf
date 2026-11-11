variable "ssh_public_key" {
  type        = string
  description = "Public key for the deploy user. One line, no quotes."

  validation {
    condition     = can(regex("^(ssh-ed25519|ssh-rsa|sk-ssh-ed25519@openssh[.]com) [A-Za-z0-9+/]+=*( [A-Za-z0-9@._-]+)?$", var.ssh_public_key))
    error_message = "ssh_public_key must be a single ssh-ed25519, sk-ssh-ed25519@openssh.com or ssh-rsa line."
  }
}

variable "ssh_port" {
  type        = number
  default     = 4822
  description = "sshd port. Port 22 is never opened."

  validation {
    condition = (
      var.ssh_port == floor(var.ssh_port) &&
      var.ssh_port >= 1025 &&
      var.ssh_port <= 32767 &&
      !contains([443, 5432, 9000, 9001], var.ssh_port)
    )
    error_message = "ssh_port must be a whole number from 1025 to 32767, not 443, 5432, 9000 or 9001."
  }
}

variable "ssh_source_addresses" {
  type        = list(string)
  description = "CIDR ranges allowed to reach sshd."

  validation {
    condition = (
      length(var.ssh_source_addresses) > 0 &&
      alltrue([
        for a in var.ssh_source_addresses :
        try(cidrhost(a, 0) == cidrhost("${split("/", a)[0]}/${strcontains(a, ":") ? 128 : 32}", 0), false)
      ])
    )
    error_message = "ssh_source_addresses needs at least one entry. Each must be a CIDR with no host bits set."
  }

  validation {
    condition = (
      var.allow_ssh_from_anywhere ||
      alltrue([for a in var.ssh_source_addresses : try(tonumber(split("/", a)[1]) >= (strcontains(a, ":") ? 32 : 8), false)])
    )
    error_message = "Prefixes shorter than /8 (IPv4) or /32 (IPv6) need allow_ssh_from_anywhere = true."
  }
}

variable "allow_ssh_from_anywhere" {
  type        = bool
  default     = false
  description = "Permits 0.0.0.0/0 or ::/0 in ssh_source_addresses."
}

variable "vpc_ip_range" {
  type        = string
  default     = "10.110.0.0/24"
  description = "VPC range. Changing it replaces every Droplet. Must not overlap another VPC in the account."

  validation {
    condition = try(
      cidrhost(var.vpc_ip_range, 0) == split("/", var.vpc_ip_range)[0] &&
      tonumber(split("/", var.vpc_ip_range)[1]) >= 16 &&
      tonumber(split("/", var.vpc_ip_range)[1]) <= 24 &&
      can(regex("^(10[.]|172[.](1[6-9]|2[0-9]|3[01])[.]|192[.]168[.])[0-9.]+/[0-9]+$", var.vpc_ip_range)),
      false
    )
    error_message = "vpc_ip_range must be an RFC 1918 IPv4 CIDR with a prefix from /16 to /24 and no host bits set."
  }
}

variable "repo_url" {
  type        = string
  description = "HTTPS clone URL of the public repo. No credentials."

  validation {
    condition     = can(regex("^https://github[.]com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+[.]git$", var.repo_url))
    error_message = "repo_url must be https://github.com/OWNER/REPO.git with plain name characters."
  }
}

variable "repo_ref" {
  type        = string
  default     = "main"
  description = "Branch or tag cloned at first boot."

  validation {
    condition     = can(regex("^[A-Za-z0-9._/-]+$", var.repo_ref))
    error_message = "repo_ref may hold letters, digits, dot, underscore, slash and hyphen only."
  }
}

variable "droplet_sizes" {
  type = map(string)
  default = {
    data   = "s-1vcpu-1gb"
    app    = "s-1vcpu-1gb"
    ingest = "s-1vcpu-512mb-10gb"
  }
  description = "Droplet size slug per role."

  validation {
    condition     = toset(keys(var.droplet_sizes)) == toset(["data", "app", "ingest"])
    error_message = "droplet_sizes keys must be exactly data, app and ingest."
  }
}
