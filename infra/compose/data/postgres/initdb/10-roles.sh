#!/usr/bin/env bash
# Runs once, on an empty data volume. Creates the migration owner, the
# run-time role and the database. Grants nothing on any table.
# Passwords come from the environment inside psql. The server does not log
# failed statements and psql prints terse errors, so no password reaches argv
# or any log.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres <<'SQL'
SET log_min_error_statement TO panic;
\set VERBOSITY terse
\getenv owner_pw PG_OWNER_PASSWORD
\getenv app_pw PG_APP_PASSWORD
CREATE ROLE refinery_owner LOGIN PASSWORD :'owner_pw' CONNECTION LIMIT 3;
CREATE ROLE refinery_app LOGIN PASSWORD :'app_pw' CONNECTION LIMIT 8;
CREATE DATABASE refinery_watch OWNER refinery_owner;
REVOKE ALL ON DATABASE refinery_watch FROM PUBLIC;
GRANT CONNECT ON DATABASE refinery_watch TO refinery_app;
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;
SQL
