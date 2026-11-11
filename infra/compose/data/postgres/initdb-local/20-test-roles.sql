-- Local test credentials, not secrets. Never used in production.
CREATE ROLE refinery_test_owner LOGIN PASSWORD 'refinery_test_owner';
CREATE ROLE refinery_test_app LOGIN PASSWORD 'refinery_test_app';
CREATE DATABASE refinery_watch_test OWNER refinery_test_owner;
REVOKE ALL ON DATABASE refinery_watch_test FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE refinery_watch_test TO refinery_test_app;
