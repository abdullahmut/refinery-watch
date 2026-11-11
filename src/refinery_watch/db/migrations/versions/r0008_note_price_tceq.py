"""note, price, tceq_event, the llm_spend view and llm_spend_usd."""

from alembic import op

from refinery_watch.db.migrations.params import grant

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

TABLES = "note, price, tceq_event"

STATEMENTS = [
    """
    CREATE TABLE note (
      id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      note_date            date NOT NULL,
      as_of                timestamptz NOT NULL,
      previous_note_id     bigint REFERENCES note(id),
      job_run_id           bigint REFERENCES job_run(id),
      built_at             timestamptz NOT NULL DEFAULT clock_timestamp(),
      status               text NOT NULL CHECK (status IN ('built','sent','send_failed',
                             'cap_reached')),
      figures              jsonb NOT NULL,
      body_text            text NOT NULL,
      body_html            text NOT NULL,
      summary              text,
      summary_model        text,
      summary_called_at    timestamptz,
      summary_tokens_in    integer CHECK (summary_tokens_in >= 0),
      summary_tokens_out   integer CHECK (summary_tokens_out >= 0),
      summary_cost_usd     numeric(10,6) CHECK (summary_cost_usd >= 0
                          AND summary_cost_usd < 'Infinity'),
      sent_at              timestamptz,
      provider_message_id  text,
      CHECK (summary IS NULL OR summary_model IS NOT NULL),
      CHECK (summary_model IS NULL OR (summary_called_at IS NOT NULL
             AND summary_tokens_in IS NOT NULL AND summary_tokens_out IS NOT NULL
             AND summary_cost_usd IS NOT NULL)),
      CHECK ((status = 'sent') = (sent_at IS NOT NULL))
    )
    """,
    "CREATE UNIQUE INDEX note_one_sent_per_day ON note (note_date) WHERE status = 'sent'",
    """
    CREATE INDEX note_summary_called_idx ON note (summary_called_at)
      WHERE summary_model IS NOT NULL
    """,
    """
    CREATE TABLE price (
      id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      series           text NOT NULL CHECK (series ~ '^[a-z0-9_.]+$'),
      observed_on      date NOT NULL,
      value            numeric(14,4) NOT NULL
                         CHECK (value > '-Infinity' AND value < 'Infinity'),
      unit             text NOT NULL,
      source_id        text NOT NULL REFERENCES source(id),
      raw_document_id  bigint REFERENCES raw_document(id),
      known_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
      UNIQUE (series, observed_on, known_at)
    )
    """,
    "CREATE INDEX price_series_idx ON price (series, observed_on, known_at DESC)",
    """
    CREATE TABLE tceq_event (
      id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      incident_id       text NOT NULL,
      report_stage      text NOT NULL CHECK (report_stage IN ('initial','final')),
      raw_document_id   bigint NOT NULL REFERENCES raw_document(id),
      refinery_id       text REFERENCES refinery(id),
      regulated_entity  text NOT NULL,
      event_kind        text NOT NULL,
      started_at        timestamptz,
      ended_at          timestamptz,
      units_text        text,
      cause             text,
      report_url        text NOT NULL,
      known_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
      UNIQUE (incident_id, report_stage)
    )
    """,
    "CREATE INDEX tceq_event_known_idx ON tceq_event (known_at)",
    """
    CREATE VIEW llm_spend AS
      SELECT 'extraction'::text AS purpose, id AS ref_id, model, called_at, tokens_in,
             tokens_out, cost_usd
      FROM extraction
      UNION ALL
      SELECT 'note_summary', id, summary_model, summary_called_at, summary_tokens_in,
             summary_tokens_out, summary_cost_usd
      FROM note WHERE summary_model IS NOT NULL
    """,
    """
    CREATE FUNCTION llm_spend_usd(from_ts timestamptz, to_ts timestamptz)
    RETURNS numeric LANGUAGE sql STABLE AS $$
      SELECT coalesce(sum(cost_usd), 0) FROM llm_spend
      WHERE called_at >= from_ts AND called_at < to_ts
    $$
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)
    op.execute(f"REVOKE ALL ON {TABLES}, llm_spend FROM PUBLIC")
    op.execute("REVOKE EXECUTE ON FUNCTION llm_spend_usd(timestamptz, timestamptz) FROM PUBLIC")
    op.execute(grant("SELECT, INSERT, UPDATE", "note"))
    op.execute(grant("SELECT, INSERT", "price, tceq_event"))
    op.execute(grant("SELECT", "llm_spend"))
    op.execute(grant("EXECUTE", "FUNCTION llm_spend_usd(timestamptz, timestamptz)"))


def downgrade() -> None:
    raise NotImplementedError("forward-only")
