# 007: Why DigitalOcean

Date: 2026-11-11

## Context

The tracker runs unattended on three small Linux VMs: two with 1 GB and one with 512 MB, on a
private network, with a firewall in front. It should sit in London, close to the European news and
regulator sources and to a refined products desk's working day. One operator builds and runs it,
so the provider must be simple to drive from Terraform and cheap at this size.

## Options

- DigitalOcean. A London region, a 512 MB size, and VPCs and cloud firewalls at no extra cost. A
  mature Terraform provider. Its cost is fewer managed services than the large clouds, which this
  project does not use.
- AWS (EC2 or Lightsail). A London region and every service there is. The costs are IAM, security
  groups and egress pricing, all of them more to get right for three VMs.
- Hetzner Cloud. Cheaper per GB of RAM, but it has no UK region.
- Akamai (Linode). A London region and similar pricing. Its firewall and VPC model is close to
  DigitalOcean's. Either would work. DigitalOcean has the simpler Terraform setup for this layout.

## Decision

DigitalOcean, region lon1. Three Droplets on one VPC, with cloud firewalls attached by tag. The
Droplets are plain Ubuntu 24.04 running Docker Compose. No managed database and no
provider-specific services.

## Consequences

The infrastructure is one small Terraform directory and the bill is predictable. Because the boxes
are plain Ubuntu with Compose, moving to another provider means rewriting `infra/terraform` only.
cloud-init, Compose and the application stay as they are.

We get no managed backups or failover. Backups and their off-box copy are our job.
