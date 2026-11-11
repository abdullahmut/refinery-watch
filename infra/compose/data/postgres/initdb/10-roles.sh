#!/usr/bin/env bash
# Runs once, on an empty data volume. Creates the migration owner, the
# run-time role and the database. Grants nothing on any table.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres \
  -v owner_pw="$PG_OWNER_PASSWORD" \
  -v app_pw="$PG_APP_PASSWORD" <<'SQL'
CREATE ROLE refinery_owner LOGIN PASSWORD :'owner_pw' CONNECTION LIMIT 3;
CREATE ROLE refinery_app LOGIN PASSWORD :'app_pw' CONNECTION LIMIT 12;
CREATE DATABASE refinery_watch OWNER refinery_owner;
REVOKE ALL ON DATABASE refinery_watch FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE refinery_watch TO refinery_app;
SQL
