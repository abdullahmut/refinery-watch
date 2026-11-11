"""Dedupe lookups for outages and restarts."""

from alembic import op

from refinery_watch.db.migrations.params import grant

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

FUNCTIONS = (
    "outage_duplicate_of(text, bigint, boolean, date, boolean, timestamptz),"
    " restart_duplicate_of(text, bigint, date, timestamptz)"
)

STATEMENTS = [
    """
    CREATE FUNCTION outage_duplicate_of(p_refinery_id text, p_refinery_unit_id bigint,
                                        p_whole_plant boolean, p_start_date date,
                                        p_start_unknown boolean, p_as_of timestamptz)
    RETURNS SETOF bigint LANGUAGE sql STABLE AS $$
      SELECT DISTINCT o.chain_id FROM outage_as_of(p_as_of) o
      WHERE o.refinery_id = p_refinery_id
        AND ((p_whole_plant AND o.whole_plant)
             OR (NOT p_whole_plant AND NOT o.whole_plant
                 AND (o.refinery_unit_id = p_refinery_unit_id
                      OR o.refinery_unit_id IS NULL
                      OR p_refinery_unit_id IS NULL)))
        AND (abs(o.start_date - p_start_date) <= 3 OR o.start_unknown OR p_start_unknown)
      ORDER BY 1
    $$
    """,
    """
    CREATE FUNCTION restart_duplicate_of(p_refinery_id text, p_refinery_unit_id bigint,
                                         p_start_date date, p_as_of timestamptz)
    RETURNS bigint LANGUAGE sql STABLE AS $$
      WITH latest AS (
        SELECT c.chain_id FROM outage_current_as_of(p_as_of) c
        WHERE c.event_type IN ('unplanned','planned')
          AND c.refinery_id = p_refinery_id
          AND c.refinery_unit_id IS NOT DISTINCT FROM p_refinery_unit_id
          AND c.start_date <= p_start_date
        ORDER BY c.start_date DESC, c.known_at DESC, c.id ASC
        LIMIT 1
      )
      SELECT r.chain_id
      FROM latest l
      JOIN outage_event t ON t.chain_id = l.chain_id
      JOIN outage_current_as_of(p_as_of) r
        ON r.event_type = 'restart' AND r.ends_event_id = t.id
      LIMIT 1
    $$
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)
    op.execute(f"REVOKE EXECUTE ON FUNCTION {FUNCTIONS} FROM PUBLIC")
    op.execute(grant("EXECUTE", f"FUNCTION {FUNCTIONS}"))


def downgrade() -> None:
    raise NotImplementedError("forward-only")
