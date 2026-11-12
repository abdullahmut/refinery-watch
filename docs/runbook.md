# Runbook

The operator runs every command here by hand. Each entry gives the symptom, the likely cause, the
commands and how to verify.

## Index

- [Provision a fresh stack](#provision-a-fresh-stack): no Droplets, or a rebuild.
- [SSH unreachable after first boot](#ssh-unreachable-after-first-boot): timeout or refusal.
- [Start Postgres on the data VM](#start-postgres-on-the-data-vm): new data Droplet.
- [Postgres down after a reboot](#postgres-down-after-a-reboot): container exited.
- [How each job connects to Postgres](#how-each-job-connects-to-postgres): which role, which
  variables, where the owner password lives.
- [Rotate a database password](#rotate-a-database-password): leaked or due.
- [Reset the laptop database](#reset-the-laptop-database): clean local start.
- [Flare signal](#flare-signal): first setup, and when a flare job fails.

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

## Start Postgres on the data Droplet

Symptom: the data Droplet is new, or Postgres is not running and nothing has started it.

Likely cause: first setup. Nothing starts the stack by itself the first time.

Put `.env` in `/opt/refinery-watch`, owned by `deploy`, mode `0600`. Set `POSTGRES_PASSWORD`,
`PG_OWNER_PASSWORD` and `PG_APP_PASSWORD` to fresh values from `openssl rand -hex 24`. Set
`DATA_BIND_ADDR` to `private_ipv4["data"]` from `terraform output private_ipv4`. It must start
with the VPC prefix `10.110.0.`. Any other value is a mistake: Postgres only accepts clients from
`10.110.0.0/24`. Then, on the Droplet:

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

- `ps` shows `postgres` as `healthy`. The check needs the `refinery_app` role, so `unhealthy` after
  a first start means the init script failed. Read `docker compose ... logs postgres`, then see
  the reset rule below.
- `ss` shows 5432 on the private address only, never `0.0.0.0` or `*`.
- `docker stats` shows memory under the 384 MiB limit.
- Write down the `available` column of `free -m`. It must be at least 500 MB.

Recorded value: none yet.

If the first init failed, the data volume now holds a cluster without roles and the script will
not run again. On a Droplet with no data yet, first check that the database is empty. This must
print `0`:

```
docker compose --env-file .env -f infra/compose/data/compose.yaml -f infra/compose/data/compose.vpc.yaml exec -T -u postgres postgres psql -d postgres -tAc "select count(*) from pg_database where datname='refinery_watch'"
```

If it prints anything else, stop. Then run `docker compose ... down`, then
`docker volume rm refinery-watch-data_pgdata`, fix the cause and run `up` again. Never do this
once the database holds data.

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

## How each job connects to Postgres

Ingest and app jobs connect as `refinery_app`. They read `PG_HOST`, `PG_PORT` (default 5432),
`PG_DATABASE` (default `refinery_watch`), `PG_APP_USER` (default `refinery_app`) and
`PG_APP_PASSWORD`. The optional `APP_DATABASE_URL` replaces all of them. Set one way or the
other, never both: the job refuses to start if both are set.

Migrations and seeds connect as `refinery_owner`. The operator supplies `PG_OWNER_PASSWORD`
(with `PG_OWNER_USER` if the role name differs), or `OWNER_DATABASE_URL`, in the shell at run
time only:

```
read -rs PG_OWNER_PASSWORD && export PG_OWNER_PASSWORD
python -m refinery_watch.db.migrate
unset PG_OWNER_PASSWORD
```

On the Droplets, the owner password is stored only in the data Droplet's `.env`. Compose passes it
to the postgres container, which keeps it in its environment, and the init script uses it once to
create the role. It is never in the ingest or app `.env`, and no job reads it from a file. The same both-set rule applies.

Each role reads only its own override. Plain `DATABASE_URL` is not read by anything. Errors name
the missing variable and never print a password or a connection string.

Verify: with only `APP_DATABASE_URL` set, `python -m refinery_watch.db.migrate` exits 2 and names
`PG_HOST`.

## Rotate a database password

Symptom: a password leaked or must change. The init script does not run again, so editing `.env`
alone changes nothing inside Postgres.

Likely cause: roles are created once, on an empty data volume.

Edit `.env` first. Then load it in a subshell, so the passwords are gone from your shell when it
ends, and set the new password inside Postgres. The
value travels in the environment and on stdin. It is never in a command line, and the session does
not log failed statements. Example for the app role, on the Droplet:

```
cd /opt/refinery-watch
(
set -a; . ./.env; set +a
docker compose --env-file .env -f infra/compose/data/compose.yaml -f infra/compose/data/compose.vpc.yaml exec -T -e PG_APP_PASSWORD -u postgres postgres psql -v ON_ERROR_STOP=1 -d postgres <<'SQL'
SET log_min_error_statement TO panic;
\set VERBOSITY terse
\getenv pw PG_APP_PASSWORD
ALTER ROLE refinery_app PASSWORD :'pw';
SQL
)
```

For the owner role, use `refinery_owner` and `PG_OWNER_PASSWORD` in both places. Then run the `up
-d` command from the start entry again, so the container environment matches `.env`. Restart every
service that connects as that role, so it reads the new value.

Verify. Type the new password at the prompt. Nothing is echoed or stored in history:

```
read -rs PGPASSWORD && export PGPASSWORD
docker compose --env-file .env -f infra/compose/data/compose.yaml -f infra/compose/data/compose.vpc.yaml exec -T -e PGPASSWORD postgres psql -U refinery_app -d refinery_watch -c 'select 1'
unset PGPASSWORD
```

The output shows one row. Then run the same three lines again and type the old password. The
output must say `password authentication failed for user "refinery_app"`.

## Reset the laptop database

Symptom: the laptop database needs a clean start, or the test roles are missing because the volume
was created before `20-test-roles.sql` existed. Init scripts run only on an empty volume.

Likely cause: the volume `refinery-watch-local_pgdata` already holds an older cluster. The laptop
stack runs as project `refinery-watch-local`, so it never shares a volume with the Droplet stack.

Laptop only. Never on the Droplet: this deletes all data.

```
make down
docker volume rm refinery-watch-local_pgdata
make up
```

An older laptop volume named `refinery-watch-data_pgdata` is no longer used. Remove it with
`docker volume rm refinery-watch-data_pgdata` if it exists.

Verify, over the container's own socket:

```
docker compose -p refinery-watch-local --env-file .env -f infra/compose/data/compose.yaml -f infra/compose/data/compose.local.yaml exec -T -e PGPASSWORD=refinery_test_owner postgres psql -U refinery_test_owner -d refinery_watch_test -c 'select 1'
```

The output shows one row with `1`.

## Create the R2 buckets and tokens

Symptom: raw documents or backups fail to upload, or the object store has never been set up.

Likely cause: first setup, a deleted bucket, or an expired token.

In the Cloudflare dashboard, open R2 and create two buckets: `refinery-watch-raw` and
`refinery-watch-backup`. Keep both private. Do not enable public access.

Create three R2 API tokens under R2, Manage API tokens. Do not choose Admin or an account-wide
scope.

- Ingest raw token: Object Read & Write on `refinery-watch-raw` only. It lives on the ingest
  Droplet.
- App raw token: Object Read on `refinery-watch-raw` only. It lives on the app Droplet. It uses the
  same `OBJECT_STORE_ACCESS_KEY` and `OBJECT_STORE_SECRET_KEY` names, with its own value.
- Backup token: Object Read & Write on `refinery-watch-backup` only. It lives on the data Droplet
  only, in `OBJECT_STORE_BACKUP_ACCESS_KEY` and `OBJECT_STORE_BACKUP_SECRET_KEY`. R2 has no object
  lock, so write access is also delete access. A break-in on the data Droplet can erase the
  backups. Keeping the token off ingest and app means a break-in there cannot.

Add a lifecycle rule on `refinery-watch-backup` under Settings, Object lifecycle rules. Delete
objects under `daily/` after 14 days. Delete objects under `monthly/` after 400 days, about 13
months. This matches record 033.

Set a 90-day TTL on all three. An expired backup token stops backups silently unless the backup
heartbeat alerts, so rotate before expiry. Copy each access key ID and secret once, straight into
`.env`. The secret is not shown again.

On each Droplet, add only the values for the token it holds. The laptop `.env` holds no backup key.
It needs no object store values at all unless you read raw documents locally. In that case give it
the app read-only raw token and nothing else. `OBJECT_STORE_BACKUP_*` exists only in the data
Droplet's `.env`. Write the values in an editor, never with `echo` or `export` on a command line:

```
OBJECT_STORE_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
OBJECT_STORE_REGION=auto
OBJECT_STORE_BUCKET_RAW=refinery-watch-raw
OBJECT_STORE_ACCESS_KEY=<raw token access key id>
OBJECT_STORE_SECRET_KEY=<raw token secret>
OBJECT_STORE_BUCKET_BACKUP=refinery-watch-backup
OBJECT_STORE_BACKUP_ACCESS_KEY=<backup token access key id>
OBJECT_STORE_BACKUP_SECRET_KEY=<backup token secret>
```

Verify on each Droplet with the token it holds. Ingest runs a put and a get with the raw
read-write token. App runs the get only with its read-only token, after the ingest put, and does not
delete the object. The data Droplet runs a put and a get with the backup token. The shell expands the
keys into a here-document that curl reads from stdin, so neither key appears in a command line or in
the process list. Run it in a subshell so the values are gone afterwards.

Ingest:

```
(
set -a; . ./.env; set +a
f=$(mktemp)
printf 'runbook check' > "$f"
curl -sS -f -K - -X PUT -T "$f" "$OBJECT_STORE_ENDPOINT/$OBJECT_STORE_BUCKET_RAW/runbook/check.txt" <<CFG
user = "$OBJECT_STORE_ACCESS_KEY:$OBJECT_STORE_SECRET_KEY"
aws-sigv4 = "aws:amz:$OBJECT_STORE_REGION:s3"
header = "x-amz-content-sha256: UNSIGNED-PAYLOAD"
CFG
curl -sS -f -K - "$OBJECT_STORE_ENDPOINT/$OBJECT_STORE_BUCKET_RAW/runbook/check.txt" <<CFG
user = "$OBJECT_STORE_ACCESS_KEY:$OBJECT_STORE_SECRET_KEY"
aws-sigv4 = "aws:amz:$OBJECT_STORE_REGION:s3"
header = "x-amz-content-sha256: UNSIGNED-PAYLOAD"
CFG
rm -f "$f"
)
```

App, get only:

```
(
set -a; . ./.env; set +a
curl -sS -f -K - "$OBJECT_STORE_ENDPOINT/$OBJECT_STORE_BUCKET_RAW/runbook/check.txt" <<CFG
user = "$OBJECT_STORE_ACCESS_KEY:$OBJECT_STORE_SECRET_KEY"
aws-sigv4 = "aws:amz:$OBJECT_STORE_REGION:s3"
header = "x-amz-content-sha256: UNSIGNED-PAYLOAD"
CFG
)
```

Data Droplet, backup token:

```
(
set -a; . ./.env; set +a
f=$(mktemp)
printf 'runbook check' > "$f"
curl -sS -f -K - -X PUT -T "$f" "$OBJECT_STORE_ENDPOINT/$OBJECT_STORE_BUCKET_BACKUP/runbook/check.txt" <<CFG
user = "$OBJECT_STORE_BACKUP_ACCESS_KEY:$OBJECT_STORE_BACKUP_SECRET_KEY"
aws-sigv4 = "aws:amz:$OBJECT_STORE_REGION:s3"
header = "x-amz-content-sha256: UNSIGNED-PAYLOAD"
CFG
curl -sS -f -K - "$OBJECT_STORE_ENDPOINT/$OBJECT_STORE_BUCKET_BACKUP/runbook/check.txt" <<CFG
user = "$OBJECT_STORE_BACKUP_ACCESS_KEY:$OBJECT_STORE_BACKUP_SECRET_KEY"
aws-sigv4 = "aws:amz:$OBJECT_STORE_REGION:s3"
header = "x-amz-content-sha256: UNSIGNED-PAYLOAD"
CFG
rm -f "$f"
)
```

A put prints nothing and a get prints `runbook check`. On app, a `403` on a put is expected, because
the token is read-only. On ingest and data, a `403` means the token scope or the keys are wrong. A
`404` on a put means the bucket name in `.env` does not match the bucket. Delete the
`runbook/check.txt` object in each bucket in the dashboard afterwards.

To rotate a token, create a new one with the same scope and TTL, edit `.env`, repeat the check,
then delete the old token in the dashboard. The ingest check covers the ingest raw token. The app
check covers the app read-only token. The data check covers the backup token.

## Flare signal

Symptom: setting up the flare signal for the first time, or one of its jobs is failing.

Likely cause: first setup, a source that robots.txt does not allow, a missing setting, or a plant
with no footprint.

The method, its limits and its status are in `docs/research/flare-lead-time.md`. The signal is a
cross-check. It is not a confirmed outage and not capacity.

### Settings

The owner adds these to `.env` by hand. Values never go in a committed file.

```
# Flare signal, ingest only.
FIRMS_MAP_KEY=
REFINERY_LOCATIONS_CSV_PATH=
# Flare alerts and the note, app only.
NOTE_FROM=
RESEND_API_KEY=
FLARE_ALERT_DAILY_MAX=10
```

`NOTE_RECIPIENTS` is already in the template. `FLARE_ALERT_DAILY_MAX` is a whole number from 1 to
50. The locations file has one row per plant:

```
refinery_id,lat,lon,radius_m,osm_ref,source_url,verified
```

`refinery_id` is the id from `refineries.csv`, in the form `R1234`. `radius_m` is 500 to 10000
and blank means 2500. `osm_ref` is blank or `way/123` or
`relation/456`. `verified` is `yes` or `no`. A bad row stops the command and names the line.

### Before the first run

Check both robots.txt files by eye. If either disallows `/api/`, stop. The fetcher will refuse the
path, and we do not work round it.

```
export CONTACT_EMAIL='<your contact address>'
UA="refinery-watch/0.1 (+mailto:$CONTACT_EMAIL)"
for h in overpass-api.de firms.modaps.eosdis.nasa.gov; do echo "=== $h"; curl --fail --location --compressed --max-time 30 -A "$UA" "https://$h/robots.txt" | head -40; sleep 5; done
curl --fail --max-time 30 -A "$UA" "https://overpass-api.de/api/status"
```

The status page should show a free slot. If not, wait.

Request a MAP_KEY in a browser at `https://firms.modaps.eosdis.nasa.gov/api/map_key/`. The key
arrives by email. Put it in the ingest `.env` as `FIRMS_MAP_KEY`. Never paste it into a file in the
repo, a command line that is logged, or a chat.

The two source rows, `osm_overpass` and `nasa_firms`, load with robots status `unchecked`. Both
jobs refuse an uncleared status. After the robots check, set the status in `data/sources.csv` to
the one that matches what you read, and load the sources again.

Save one real reply of each kind so the parser tests can run on real files. Put them under
`tests/fixtures/sources/overpass/` and `tests/fixtures/sources/firms/`. Check that the key is not in
any saved file:

```
read -rs FIRMS_MAP_KEY
grep -c -F -f <(printf '%s\n' "$FIRMS_MAP_KEY") tests/fixtures/sources/firms/* || true
unset FIRMS_MAP_KEY
```

The count must be 0 for every file. Compare the real column names with the list in
`flare/firms.py`. The parser fails on a reply that lacks them, and says so.

### First load

Run these in order, by hand.

1. Compile `refinery_locations.csv` for the plants in `refineries.csv`. Put it where
   `REFINERY_LOCATIONS_CSV_PATH` points on ingest.
2. Load the two source rows with the sources seed.
3. `python -m refinery_watch.flare footprints` on ingest. It does at most 100 plants a run, new
   plants first. Repeat until every plant has a footprint. Count the `point_radius` rows in
   `plant_footprint`. Those plants use a circle round the point, and every output says so.
4. `python -m refinery_watch.flare backfill --from <today minus 455 days> --to <yesterday>`.
   Each run takes at most 150 requests and picks up where the last one stopped. Repeat until the
   job note stops saying requests are left. The backfill uses the FIRMS data availability list
   first, so the key must work.
5. `python -m refinery_watch.flare research --out docs/research/flare-lead-time-results.md`. Then
   `calibrate --kind signal` and `calibrate --kind alert`. Read the curves. Add `--write` only if
   you are happy with the chosen value. `calibrate` refuses with exit code 3 when there are fewer
   than 1,000 quiet scored plant-days.
6. `python -m refinery_watch.flare alert` on app. This is a dry run. It prints the subject and body
   of each alert that would go out, and writes and sends nothing. Read the output.
7. Ask platform to add the cron lines: `flare fetch` on ingest every 3 hours at minute 40 under the
   ingest job lock, and `flare alert --send` on app every 30 minutes with its own lock and a 128 MB
   limit.

Exit codes: 0 ok, 1 failed, 2 setting or usage, 3 refused. Each job writes a `job_run` row.

### When it fails

- **The job says the robots status is not a clear allow.** The source row is still `unchecked`.
  Check robots.txt by eye, then change the row. Do not edit the database by hand.
- **`host_blocked` or a 4xx in the note.** The host refused us. The job stops for that host. Do not
  retry in a loop. Wait, and read the host's usage page.
- **`no footprints; run the footprints command first`.** Run step 3 above.
- **A plant never gets a score.** A day needs both satellites fetched and 60 scored days before
  it. Check `flare_coverage` for the plant, then run the backfill again.
- **No alert came.** Run `alert` without `--send` and read the candidates. A plant gets one alert
  per Dubai day, and at most `FLARE_ALERT_DAILY_MAX` go out in total. The log line for a plant
  that was held back says `capped`.
- **The send failed.** The job retries the stored email on each run, up to 3 attempts in 18 hours,
  with the same key. A changed `NOTE_RECIPIENTS` or `NOTE_FROM` refuses the retry. Put the old
  value back, or wait for the 18 hours to pass.
- **The key may have leaked.** The key sits in the request path, so it must never be in a log, a
  note or a stored URL. If it appears anywhere, request a new MAP_KEY and replace the old one.

### Limits

The fetch holds one FIRMS reply at a time, 5 MB at most, inside the 192 MB container limit specified
for each ingest job, which is not set in Compose yet. Nothing else runs at the same time. The alert job runs on app with a
128 MB limit. No statistics run in Python.
