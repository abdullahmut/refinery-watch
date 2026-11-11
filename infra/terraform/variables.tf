variable "ssh_public_key" {
  type        = string
  description = "Public key for the deploy user. One line, no quotes."

  validation {
    condition     = can(regex("^(ssh-ed25519|ssh-rsa) [A-Za-z0-9+/]+=*( [A-Za-z0-9@._-]+)?$", var.ssh_public_key))
    error_message = "ssh_public_key must be a single ssh-ed25519 or ssh-rsa line."
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
    condition     = length(var.ssh_source_addresses) > 0 && alltrue([for a in var.ssh_source_addresses : can(cidrhost(a, 0))])
    error_message = "ssh_source_addresses needs at least one entry and each must be a CIDR."
  }
}

variable "vpc_ip_range" {
  type        = string
  default     = "10.110.0.0/24"
  description = "VPC range. Must not overlap another VPC in the account."

  validation {
    condition     = can(cidrhost(var.vpc_ip_range, 0))
    error_message = "vpc_ip_range must be a CIDR."
  }
}

variable "repo_url" {
  type        = string
  description = "HTTPS clone URL of the repo. No credentials."

  validation {
    condition     = can(regex("^https://[^@/[:space:]]+/[^@[:space:]]+[.]git$", var.repo_url))
    error_message = "repo_url must be an https URL ending in .git, with no @."
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
    condition     = length(setsubtract(keys(var.droplet_sizes), ["data", "app", "ingest"])) == 0 && length(keys(var.droplet_sizes)) == 3
    error_message = "droplet_sizes keys must be exactly data, app and ingest."
  }
}
