"""Reference tables: refinery, refinery_unit, refinery_status_change, source."""

from alembic import op

from refinery_watch.db.migrations.params import app_role, grant, schema

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TABLES = "refinery, refinery_unit, refinery_status_change, source"

STATEMENTS = [
    """
    CREATE TABLE refinery (
      id             text PRIMARY KEY CHECK (id ~ '^R[0-9]{4}$'),
      name           text NOT NULL CHECK (name <> ''),
      aliases        text[] NOT NULL DEFAULT '{}',
      country        text NOT NULL,
      region         text NOT NULL CHECK (region IN ('US Gulf','US other','NW Europe','Med',
                       'Middle East','Singapore','NE Asia','India','China','Other')),
      owner          text,
      nameplate_kbd  numeric(7,1) CHECK (nameplate_kbd > 0 AND nameplate_kbd < 'Infinity'),
      status         text NOT NULL CHECK (status IN ('operating','closed_or_converted')),
      source_url     text,
      verified       boolean NOT NULL DEFAULT false,
      verified_on    date,
      notes          text,
      loaded_at      timestamptz NOT NULL DEFAULT now(),
      CHECK (verified_on IS NULL OR verified)
    )
    """,
    """
    CREATE TABLE refinery_unit (
      id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      refinery_id   text NOT NULL REFERENCES refinery(id),
      unit_code     text NOT NULL CHECK (unit_code IN ('CDU','VDU','FCC','HCU','COK','REF',
                      'HDT_D','HDT_G','ALK','ISOM','SRU','H2','RHU')),
      unit_tag      text NOT NULL DEFAULT '',
      capacity_kbd  numeric(7,1) CHECK (capacity_kbd > 0 AND capacity_kbd < 'Infinity'),
      UNIQUE (refinery_id, unit_code, unit_tag),
      UNIQUE (id, refinery_id)
    )
    """,
    """
    CREATE TABLE refinery_status_change (
      id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      refinery_id  text NOT NULL REFERENCES refinery(id),
      old_status   text NOT NULL,
      new_status   text NOT NULL,
      logged_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
      CHECK (old_status <> new_status)
    )
    """,
    "CREATE INDEX refinery_status_change_logged_idx ON refinery_status_change (logged_at)",
    """
    CREATE TABLE source (
      id                   text PRIMARY KEY CHECK (id ~ '^[a-z0-9_]+$'),
      name                 text NOT NULL,
      tier                 smallint NOT NULL CHECK (tier BETWEEN 1 AND 3),
      region_focus         text NOT NULL,
      access_method        text NOT NULL CHECK (access_method IN ('rss','html','html_or_open_data',
                             'api','bulk_csv','pdf_monthly')),
      feed_url             text NOT NULL,
      article_url_pattern  text NOT NULL,
      robots_status        text NOT NULL,
      fetch_interval_min   integer NOT NULL CHECK (fetch_interval_min > 0),
      notes                text NOT NULL DEFAULT '',
      verified             boolean NOT NULL DEFAULT false,
      loaded_at            timestamptz NOT NULL DEFAULT now()
    )
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)
    op.execute(f"REVOKE ALL ON {TABLES} FROM PUBLIC")
    op.execute(f'GRANT USAGE ON SCHEMA {schema()} TO "{app_role()}"')
    op.execute(grant("SELECT", TABLES))


def downgrade() -> None:
    raise NotImplementedError("forward-only")
