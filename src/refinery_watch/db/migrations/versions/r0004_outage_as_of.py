"""outage_as_of with no lookahead, and match_restart."""

from alembic import op

from refinery_watch.db.migrations.params import grant, pin_search_path

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

FUNCTIONS = (
    "outage_current_as_of(timestamptz), outage_as_of(timestamptz),"
    " match_restart(text, bigint, date, timestamptz)"
)

STATEMENTS = [
    """
    CREATE FUNCTION outage_current_as_of(as_of timestamptz)
    RETURNS SETOF outage_event LANGUAGE sql STABLE AS $$
      SELECT e.* FROM outage_event e
      WHERE e.known_at <= as_of
        AND NOT EXISTS (SELECT 1 FROM outage_event s
                        WHERE s.supersedes = e.id AND s.known_at <= as_of)
    $$
    """,
    """
    CREATE FUNCTION outage_as_of(as_of timestamptz)
    RETURNS SETOF outage_event LANGUAGE sql STABLE AS $$
      SELECT c.* FROM outage_current_as_of(as_of) c
      WHERE c.event_type IN ('unplanned','planned')
        AND NOT EXISTS (SELECT 1
                        FROM outage_current_as_of(as_of) r
                        JOIN outage_event t ON t.id = r.ends_event_id
                        WHERE r.event_type = 'restart' AND t.chain_id = c.chain_id)
    $$
    """,
    """
    CREATE FUNCTION match_restart(p_refinery_id text, p_refinery_unit_id bigint,
                                  p_start_date date, p_as_of timestamptz)
    RETURNS bigint LANGUAGE sql STABLE AS $$
      SELECT o.id FROM outage_as_of(p_as_of) o
      WHERE o.refinery_id = p_refinery_id
        AND o.refinery_unit_id IS NOT DISTINCT FROM p_refinery_unit_id
        AND o.start_date <= p_start_date
      ORDER BY o.start_date DESC, o.known_at DESC, o.id ASC
      LIMIT 1
    $$
    """,
]

PINNED = (
    "outage_current_as_of(timestamptz)",
    "outage_as_of(timestamptz)",
    "match_restart(text, bigint, date, timestamptz)",
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)
    for statement in pin_search_path(*PINNED):
        op.execute(statement)
    op.execute(f"REVOKE EXECUTE ON FUNCTION {FUNCTIONS} FROM PUBLIC")
    op.execute(grant("EXECUTE", f"FUNCTION {FUNCTIONS}"))


def downgrade() -> None:
    raise NotImplementedError("forward-only")
