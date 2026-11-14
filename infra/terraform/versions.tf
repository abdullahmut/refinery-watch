terraform {
  required_version = ">= 1.17.0, < 1.18.0"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "= 2.107.0"
    }
  }
}
