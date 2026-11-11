# Runbook

The operator runs every command here by hand. Each entry gives the symptom, the likely cause, the
commands and how to verify.

## Provision a fresh stack

Symptom: no Droplets exist, or the stack was destroyed and must be rebuilt.

Likely cause: first setup, or a deliberate destroy and apply.

Commands, from the repo root on the operator's laptop:

```
cp infra/terraform/example.tfvars infra/terraform/terraform.tfvars
```

Edit `terraform.tfvars`. Set `ssh_public_key`, `ssh_source_addresses` and `repo_url`. Set
`repo_ref` to a release tag once one exists. The file is ignored by git and refused by the
pre-commit hook. Never put the API token in it.

```
export DIGITALOCEAN_TOKEN=...
cd infra/terraform
terraform init
terraform plan
terraform apply
terraform output
```

The plan must show a VPC, an SSH key, four tags, three firewalls and three Droplets, and nothing
else. After apply, copy `terraform.tfstate` to one place off the laptop and outside this repo
(record 006).

Check straight away that port 22 is closed. The command must time out:

```
nc -vz -w 5 <public ip> 22
```

Wait two to three minutes for cloud-init, then on each Droplet:

```
ssh -p <ssh_port> deploy@<public ip>
cloud-init status --wait
sudo sshd -T | grep -Ei '^(port|passwordauthentication|permitrootlogin)'
sudo fail2ban-client status sshd
systemctl is-active unattended-upgrades docker
docker compose version
git -C /opt/refinery-watch log -1
free -m
```

Verify:

- `cloud-init status` says `done`.
- `sshd -T` shows `port <ssh_port>`, `passwordauthentication no`, `permitrootlogin no`.
- `fail2ban-client status sshd` lists the jail.
- Both services print `active`.
- The checkout is at `repo_ref`.
- On ingest, `free -m` shows at least 250 MB available. Write the number here: not recorded yet.
- `ssh -p <ssh_port> root@<public ip>` is refused.
- `nc -vz -w 5 <data public ip> 5432` and `9000` time out.

Changes to cloud-init, `ssh_public_key`, `ssh_port` or `repo_ref` reach only new Droplets. The
Droplet resource ignores `user_data` and `ssh_keys`, so editing them never destroys a running box.
To apply a change to one box, replace it on purpose:

```
terraform apply -replace='digitalocean_droplet.node["ingest"]'
```

Replacing the data Droplet deletes its disk. Do not do that once it holds data.

If apply fails on the SSH key with a duplicate error, the same key is already uploaded to the
account. Delete the old upload in the DigitalOcean control panel, or import it with
`terraform import digitalocean_ssh_key.operator <fingerprint>`.

## SSH unreachable after first boot

Symptom: `ssh -p <ssh_port> deploy@<public ip>` times out or is refused after apply.

Likely cause, in order:

1. The laptop's address is not in `ssh_source_addresses`. The cloud firewall drops it.
2. cloud-init failed before it moved sshd to `ssh_port`. Port 22 is never open, so the box has no
   reachable SSH.
3. sshd stayed on port 22 because the socket unit did not pick up the new port.

Check cause 1 first. Find the current address, then compare it with the list:

```
curl -s https://ifconfig.me
grep ssh_source_addresses infra/terraform/terraform.tfvars
```

If the address is missing, add it to `terraform.tfvars` and run `terraform apply`. Firewall rules
change in place and no Droplet is touched.

For causes 2 and 3, use the DigitalOcean control panel. Open the Droplet, choose Access, then
Reset root password, then Launch Recovery Console. Log in as root with the emailed password. The
reset is a temporary way in and nothing else uses it. Then run, as root:

```
cloud-init status --long
journalctl -u cloud-init --no-pager | tail -n 80
sshd -t
systemctl is-enabled ssh.socket
ss -ltnp | grep sshd
```

To move sshd by hand, if `sshd -t` is clean:

```
systemctl daemon-reload
systemctl restart ssh.socket
```

If `ssh.socket` is not enabled, run `systemctl restart ssh.service` instead. If a package step
failed, fix the cause shown by `journalctl`, then run `cloud-init clean --logs --reboot` only on a
box that holds no data.

Verify: from the laptop, `ssh -p <ssh_port> deploy@<public ip>` logs in, and
`nc -vz -w 5 <public ip> 22` times out.
