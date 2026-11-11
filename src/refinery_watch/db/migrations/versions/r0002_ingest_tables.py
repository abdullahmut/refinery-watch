"""Ingest tables: job_run, raw_document, extraction, review_item."""

from alembic import op

from refinery_watch.db.migrations.params import grant

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLES = "job_run, raw_document, extraction, review_item"

STATEMENTS = [
    """
    CREATE TABLE job_run (
      id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      job          text NOT NULL CHECK (job ~ '^[a-z0-9_]+$'),
      started_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
      finished_at  timestamptz,
      status       text NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running','ok','failed','skipped')),
      rows_in      integer CHECK (rows_in >= 0),
      rows_out     integer CHECK (rows_out >= 0),
      error        text,
      CHECK ((status = 'running') = (finished_at IS NULL))
    )
    """,
    "CREATE UNIQUE INDEX job_run_one_running ON job_run (job) WHERE status = 'running'",
    "CREATE INDEX job_run_job_started_idx ON job_run (job, started_at DESC)",
    """
    CREATE TABLE raw_document (
      id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      source_id         text NOT NULL REFERENCES source(id),
      job_run_id        bigint REFERENCES job_run(id),
      url               text NOT NULL,
      content_sha256    text NOT NULL UNIQUE CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
      object_key        text NOT NULL UNIQUE,
      content_type      text,
      headline          text,
      body_chars        integer NOT NULL CHECK (body_chars >= 0),
      fetched_at        timestamptz NOT NULL DEFAULT now(),
      published_at      timestamptz,
      published_tz      text,
      publication_date  date GENERATED ALWAYS AS
                          ((coalesce(published_at, fetched_at)
                            AT TIME ZONE coalesce(published_tz, 'UTC'))::date) STORED,
      prefilter_passed  boolean NOT NULL,
      prefilter_terms   text[] NOT NULL DEFAULT '{}',
      CHECK (published_tz IS NULL OR published_at IS NOT NULL),
      CHECK (prefilter_passed = (cardinality(prefilter_terms) > 0))
    )
    """,
    "CREATE INDEX raw_document_url_idx ON raw_document (url)",
    "CREATE INDEX raw_document_fetched_idx ON raw_document (fetched_at)",
    """
    CREATE TABLE extraction (
      id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      raw_document_id  bigint NOT NULL REFERENCES raw_document(id),
      job_run_id       bigint REFERENCES job_run(id),
      model            text NOT NULL,
      prompt_version   text NOT NULL CHECK (prompt_version ~ '^extract_v[0-9]+$'),
      called_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
      tokens_in        integer NOT NULL CHECK (tokens_in >= 0),
      tokens_out       integer NOT NULL CHECK (tokens_out >= 0),
      cost_usd         numeric(10,6) NOT NULL CHECK (cost_usd >= 0),
      status           text NOT NULL CHECK (status IN ('ok','invalid_output','api_error')),
      raw_output       text,
      output           jsonb,
      error            text,
      CHECK (status <> 'ok' OR output IS NOT NULL),
      CHECK (status = 'ok' OR error IS NOT NULL)
    )
    """,
    """
    CREATE UNIQUE INDEX extraction_once_per_prompt ON extraction (raw_document_id, prompt_version)
      WHERE status = 'ok'
    """,
    "CREATE INDEX extraction_called_idx ON extraction (called_at)",
    """
    CREATE TABLE review_item (
      id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      raw_document_id  bigint NOT NULL REFERENCES raw_document(id),
      extraction_id    bigint REFERENCES extraction(id),
      candidate        jsonb NOT NULL,
      reason           text NOT NULL CHECK (reason ~ '^[a-z_]+$'),
      detail           text,
      created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
      resolved_at      timestamptz,
      resolution       text CHECK (resolution IN ('written','dismissed')),
      outage_event_id  bigint,
      CHECK ((resolved_at IS NULL) = (resolution IS NULL)),
      CHECK (resolution IS DISTINCT FROM 'written' OR outage_event_id IS NOT NULL)
    )
    """,
    "CREATE INDEX review_item_open_idx ON review_item (created_at) WHERE resolved_at IS NULL",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)
    op.execute(f"REVOKE ALL ON {TABLES} FROM PUBLIC")
    op.execute(grant("SELECT, INSERT", "raw_document, extraction"))
    op.execute(grant("SELECT, INSERT, UPDATE", "job_run, review_item"))


def downgrade() -> None:
    raise NotImplementedError("forward-only")
