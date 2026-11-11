# Runbook

The operator runs every command here by hand. Each entry gives the symptom, the likely cause, the
commands and how to verify.

## Provision a fresh stack

Symptom: no Droplets exist, or the stack was destroyed and must be rebuilt.

Likely cause: first setup, or a deliberate destroy and apply.

Create the API token in the DigitalOcean control panel as a custom-scope token with an expiry of 30
to 90 days. Grant these scopes: `droplet:create/read/update/delete`,
`vpc:create/read/update/delete`, `firewall:create/read/update/delete`,
`ssh_key:create/read/update/delete`, `tag:create/read/delete`, `image:read`, `regions:read`,
`sizes:read` and `actions:read`. Check the list against the token screen. Grant nothing else, and
do not use a full-access token.

Create `infra/terraform/terraform.tfvars` with these keys. The file is ignored by git and refused
by the pre-commit hook. Never put the API token in it. Until `example.tfvars` exists, use this
template:

```
ssh_public_key       = "ssh-ed25519 AAAAREPLACEWITHYOURPUBLICKEY operator@laptop"
ssh_source_addresses = ["203.0.113.10/32"]
repo_url             = "https://github.com/OWNER/refinery-watch.git"
repo_ref             = "main"
```

The template uses `main` because no tag exists yet. Set `repo_ref` to a release tag before any
deploy. A branch name moves under you. Checking tag
signatures is the deploy spec's job, not this runbook's. Use `0.0.0.0/0` in
`ssh_source_addresses` only with `allow_ssh_from_anywhere = true`.

Never put a secret in cloud-init or in `user_data`. Any process on the box can read it from the
metadata service.

Then, from the repo root:

```
read -rs DIGITALOCEAN_TOKEN && export DIGITALOCEAN_TOKEN
cd infra/terraform
terraform init -lockfile=readonly
terraform plan
terraform apply
terraform output
```

The plan must show a VPC, an SSH key, four tags, four firewalls and three Droplets, and nothing
else. After apply, copy `terraform.tfstate` to one encrypted place off the laptop and outside this
repo (record 006).

Check straight away that port 22 is closed. The command must time out:

```
nc -vz -w 5 <public ip> 22
```

Wait for cloud-init. The sshd move runs after the package installs, so SSH may not answer for
several minutes. Poll the port until it answers:

```
nc -vz -w 5 <public ip> <ssh_port>
```

The first connection trusts the host key on first use. To check it, open the Recovery Console
and compare `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` with the fingerprint ssh prints.
Opening the console resets the root password and power-cycles the box. The steps are under "SSH
unreachable after first boot".

Then on each Droplet:

```
ssh -p <ssh_port> deploy@<public ip>
cloud-init status --wait
sudo sshd -T | grep -Ei '^(port|passwordauthentication|permitrootlogin)'
sudo fail2ban-client status sshd
systemctl is-active docker apt-daily-upgrade.timer
docker compose version
git -C /opt/refinery-watch log -1
free -m
```

Verify:

- `cloud-init status` says `done`.
- `sshd -T` shows `port <ssh_port>`, `passwordauthentication no`, `permitrootlogin no`.
- `fail2ban-client status sshd` lists the jail.
- Both units print `active`.
- The checkout is at `repo_ref`.
- On ingest, `free -m` shows at least 250 MB available. Recorded value: none yet.
- `ssh -p <ssh_port> root@<public ip>` is refused.
- `ssh -o PubkeyAuthentication=no -p <ssh_port> deploy@<public ip>` is refused.
- `nc -vz -w 5 <data public ip> 5432` times out.

### What changes reach running Droplets

The Droplet resource ignores `user_data` and `ssh_keys`. Editing `ssh_public_key`, `repo_url`,
`repo_ref` or the cloud-init files changes nothing on a running box and never destroys one. They
reach only new Droplets.

Changing `ssh_port` moves the firewall on running Droplets at once. Change it only together with
`-replace` on every Droplet, or move sshd by hand first. If the firewall moves first, sshd is still
on the old port and every box is unreachable. Use the Recovery Console entry below to recover.

Changing a size in `droplet_sizes` resizes the box in place and powers it off. Do it in a quiet
window, and stop the Compose stack on the box first.

Changing `vpc_ip_range` replaces the VPC, so it replaces all three Droplets, including the data
disk. Do not change it once the data box holds data.

To apply a cloud-init change to one box, replace it on purpose:

```
terraform apply -replace='digitalocean_droplet.node["ingest"]'
```

Replacing the data Droplet deletes its disk. Do not do that once it holds data.

If apply fails on the SSH key with a duplicate error, the same key is already uploaded to the
account. Delete the old upload in the control panel, or import it by its numeric ID:

```
doctl compute ssh-key list
terraform import digitalocean_ssh_key.operator <numeric key id>
```

### Weekly reboot

Unattended upgrades install kernel fixes but never reboot. Once a week, on each Droplet:

```
ls /var/run/reboot-required
```

If the file exists, reboot outside 01:00 to 03:00 UTC, which holds the note job. Do ingest and app
first and data last, and wait for each to answer on SSH before the next:

```
sudo reboot
```

Verify with `uptime` after login.

## SSH unreachable after first boot

Symptom: `ssh -p <ssh_port> deploy@<public ip>` times out or is refused after apply.

Likely cause, in order:

1. The laptop's address is not in `ssh_source_addresses`. The cloud firewall drops it.
2. cloud-init has not finished, or failed before it moved sshd to `ssh_port`. Port 22 is never
   open, so the box has no reachable SSH.
3. sshd stayed on port 22 because the socket unit did not pick up the new port.

Check cause 1 first. Find the current IPv4 address, then compare it with the list:

```
curl -4 -s https://ifconfig.me
grep ssh_source_addresses infra/terraform/terraform.tfvars
```

If the address is missing, add it to `terraform.tfvars` and run `terraform apply`. Firewall rules
change in place and no Droplet is touched.

For causes 2 and 3, use the DigitalOcean control panel. Root has no password, so there are
two ways in.

With a password reset: open the Droplet, choose Access, then Reset root password, then Launch
Recovery Console. The reset power-cycles the box. If the data Droplet is involved, stop the Compose
stack on it first if you can, so Postgres shuts down cleanly. Log in as root with the emailed
password, then run the repair below. When done, lock the account again and delete the email:

```
passwd -l root
passwd -S root
```

`passwd -S root` must show `L` as the second field.

Without a password: power the Droplet off, boot it from the Recovery ISO in the control panel,
mount the root disk, fix the drop-in in `/etc/ssh/sshd_config.d/10-refinery-watch.conf`, and boot
from disk again.

Repair, as root:

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

If `ssh.socket` is not enabled, run `systemctl restart ssh.service` instead.

If a package step failed, fix the cause shown by `journalctl` and run the failed step by hand.
Do not run `cloud-init clean`. It re-runs `runcmd` on the next boot, and the clone fails because
`/opt/refinery-watch` already exists. To rebuild from scratch, replace the Droplet instead, as
above, on a box that holds no data.

Verify: from the laptop, `ssh -p <ssh_port> deploy@<public ip>` logs in, and
`nc -vz -w 5 <public ip> 22` times out.

## Start Postgres on the data VM

Symptom: the data Droplet is new, or Postgres is not running and nothing has started it.

Likely cause: first setup. Nothing starts the stack by itself the first time.

Put `.env` in `/opt/refinery-watch`, owned by `deploy`, mode `0600`. Set `POSTGRES_PASSWORD`,
`PG_OWNER_PASSWORD` and `PG_APP_PASSWORD` to fresh values from `openssl rand -hex 24`. Set
`DATA_BIND_ADDR` to `private_ipv4["data"]` from `terraform output private_ipv4`. Then, on the
Droplet:

```
cd /opt/refinery-watch
stat -c '%U %a' .env
docker compose --env-file .env -f infra/compose/data/compose.yaml -f infra/compose/data/compose.vpc.yaml up -d --wait
```

`stat` must print `deploy 600`. If `DATA_BIND_ADDR` is empty, Compose stops and says to set it.

Verify:

```
docker compose --env-file .env -f infra/compose/data/compose.yaml -f infra/compose/data/compose.vpc.yaml ps
ss -ltn | grep 5432
docker stats --no-stream
free -m
```

- `ps` shows `postgres` as `healthy`.
- `ss` shows 5432 on the private address only, never `0.0.0.0` or `*`.
- `docker stats` shows memory under the 384 MiB limit.
- Write down the `available` column of `free -m`. It must be at least 500 MB.

## Postgres down after a reboot

Symptom: after a reboot, `docker ps` does not list `postgres`, or `ps` shows it exited.

Likely cause: Docker started before the private address was up, so the port bind failed. The
restart policy does not retry a failed bind.

Fix, on the Droplet:

```
cd /opt/refinery-watch
docker compose --env-file .env -f infra/compose/data/compose.yaml -f infra/compose/data/compose.vpc.yaml up -d --wait
```

Verify as in the entry above: `postgres` is `healthy` and `ss -ltn | grep 5432` shows the private
address. If `up` still fails on the bind, check `ip -4 addr show eth1` for the private address and
compare it with `DATA_BIND_ADDR`.

## Rotate a database password

Symptom: a password leaked or must change. The init script does not run again, so editing `.env`
alone changes nothing inside Postgres.

Likely cause: roles are created once, on an empty data volume.

Edit `.env` first. Then set the new password in Postgres. This reads the value from `.env` on the
Droplet and never prints it. Example for the app role:

```
cd /opt/refinery-watch
set -a; . ./.env; set +a
docker compose --env-file .env -f infra/compose/data/compose.yaml -f infra/compose/data/compose.vpc.yaml exec -u postgres postgres psql -v pw="$PG_APP_PASSWORD" -c "ALTER ROLE refinery_app PASSWORD :'pw'"
```

Use `refinery_owner` and `PG_OWNER_PASSWORD` for the owner role. Then restart every service that
connects as that role, so it reads the new value.

Verify: connect with the new password from the app Droplet, and confirm the old one is refused:

```
PGPASSWORD=<new> psql -h <data private ip> -U refinery_app -d refinery_watch -c 'select 1'
```

## Reset the laptop database

Symptom: the laptop database needs a clean start, or the test roles are missing because the volume
was created before `20-test-roles.sql` existed. Init scripts run only on an empty volume.

Likely cause: the volume `refinery-watch-data_pgdata` already holds an older cluster.

Laptop only. Never on the Droplet: this deletes all data.

```
make down
docker volume rm refinery-watch-data_pgdata
make up
```

Verify:

```
psql "postgresql://refinery_test_owner:refinery_test_owner@127.0.0.1:5432/refinery_watch_test" -c "select 1"
```
