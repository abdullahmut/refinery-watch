# 006: Terraform state location

Date: 2026-11-11

## Context

Terraform keeps a state file that maps the config to the real Droplets, VPC and firewalls. One
operator runs Terraform by hand from one laptop, a few times a month at most. The file must never
be committed, because the repo is public, and losing it means importing every resource again by
hand.

## Options

- Local file on the operator's laptop. Nothing to set up and no extra account or keys. The cost is
  that the file lives on one disk, and there is no locking, which one operator does not need.
- DigitalOcean Spaces with the S3 backend. Safe from a lost laptop. It costs a monthly fee and a
  second set of access keys, and the bucket has to exist before Terraform can use it.
- A hosted Terraform service. It adds remote state and locking, but also a third-party account and
  a token for a three-VM project.
- MinIO on the data Droplet. Circular: the state of a box would live on that box.

## Decision

Local state in `infra/terraform/terraform.tfstate`. It holds no token, because the provider reads
`DIGITALOCEAN_TOKEN` from the shell. The ignore file covers `*.tfstate` and `*.tfstate.*`, and the
local pre-commit hook refuses any staged state file. After each apply, the operator copies the
state file to one encrypted place off the laptop that is not this repo, such as a password manager
or an encrypted disk.

## Consequences

Setup stays at zero and there is nothing new to secure. Only one person can run Terraform at a
time, and that is the case today. A lost laptop without the copy means re-importing resources.

To undo: add a `backend "s3"` block pointing at a Spaces bucket and run
`terraform init -migrate-state`. Nothing else in the config changes.
