"""Stamp known_at from the database clock on tceq_event and price, and make both append-only."""

from alembic import op

from refinery_watch.db.migrations.params import pin_search_path, revoke

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

TABLE_NAMES = ("tceq_event", "price")
TABLES = ", ".join(TABLE_NAMES)

STAMP_FUNCTION = """
CREATE FUNCTION stamp_known_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.known_at := clock_timestamp();
  RETURN NEW;
END
$$
"""

PINNED = ("stamp_known_at()",)


def upgrade() -> None:
    op.execute(STAMP_FUNCTION)
    for statement in pin_search_path(*PINNED):
        op.execute(statement)
    op.execute("REVOKE EXECUTE ON FUNCTION stamp_known_at() FROM PUBLIC")
    for table in TABLE_NAMES:
        op.execute(
            f"CREATE TRIGGER {table}_before_insert BEFORE INSERT ON {table}"
            " FOR EACH ROW EXECUTE FUNCTION stamp_known_at()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}"
            " FOR EACH ROW EXECUTE FUNCTION outage_append_only()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table}"
            " FOR EACH STATEMENT EXECUTE FUNCTION outage_append_only()"
        )
    op.execute(revoke("UPDATE, DELETE, TRUNCATE", TABLES))


def downgrade() -> None:
    raise NotImplementedError("forward-only")
