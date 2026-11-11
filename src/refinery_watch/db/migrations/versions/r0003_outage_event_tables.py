"""outage_event and event_source: append-only, primary source required."""

from alembic import op

from refinery_watch.db.migrations.params import grant, revoke

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TABLES = "outage_event, event_source"

STATEMENTS = [
    """
    CREATE TABLE outage_event (
      id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      chain_id               bigint NOT NULL REFERENCES outage_event(id),
      supersedes             bigint REFERENCES outage_event(id),
      ends_event_id          bigint REFERENCES outage_event(id),
      event_type             text NOT NULL CHECK (event_type IN ('unplanned','planned','restart')),
      refinery_id            text NOT NULL REFERENCES refinery(id),
      refinery_unit_id       bigint,
      whole_plant            boolean NOT NULL DEFAULT false,
      unit_label             text,
      capacity_affected_kbd  numeric(7,1) CHECK (capacity_affected_kbd > 0),
      capacity_basis         text NOT NULL CHECK (capacity_basis IN ('stated','unit_reference',
                               'plant_nameplate','not_stated')),
      start_date             date NOT NULL,
      start_unknown          boolean NOT NULL DEFAULT false,
      old_start              boolean NOT NULL DEFAULT false,
      expected_restart_date  date,
      cause                  text,
      confidence             text NOT NULL CHECK (confidence IN ('high','medium','low')),
      match_score            smallint NOT NULL CHECK (match_score BETWEEN 0 AND 100),
      known_at               timestamptz NOT NULL,
      CONSTRAINT outage_event_supersedes_key UNIQUE (supersedes),
      CONSTRAINT outage_event_unit_fk FOREIGN KEY (refinery_unit_id, refinery_id)
        REFERENCES refinery_unit (id, refinery_id),
      CHECK (NOT whole_plant OR refinery_unit_id IS NULL),
      CHECK ((supersedes IS NULL) = (chain_id = id)),
      CHECK ((event_type = 'restart') = (ends_event_id IS NOT NULL)),
      CHECK (event_type <> 'restart' OR (expected_restart_date IS NULL AND NOT start_unknown
                                         AND NOT old_start)),
      CHECK ((capacity_basis = 'not_stated') = (capacity_affected_kbd IS NULL)),
      CHECK (NOT start_unknown OR confidence <> 'high'),
      CHECK (NOT old_start OR confidence = 'low')
    )
    """,
    "CREATE INDEX outage_event_known_idx ON outage_event (known_at)",
    "CREATE INDEX outage_event_chain_idx ON outage_event (chain_id)",
    """
    CREATE INDEX outage_event_ends_idx ON outage_event (ends_event_id)
      WHERE ends_event_id IS NOT NULL
    """,
    "CREATE INDEX outage_event_unit_idx ON outage_event (refinery_id, refinery_unit_id)",
    """
    CREATE TABLE event_source (
      id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      outage_event_id  bigint NOT NULL REFERENCES outage_event(id),
      raw_document_id  bigint NOT NULL REFERENCES raw_document(id),
      extraction_id    bigint REFERENCES extraction(id),
      role             text NOT NULL CHECK (role IN ('primary','corroboration')),
      source_kind      text NOT NULL CHECK (source_kind IN ('company_statement','regulator',
                         'named_official','anonymous_sources','other_media','unclear')),
      evidence         text NOT NULL CHECK (evidence <> ''),
      known_at         timestamptz NOT NULL,
      UNIQUE (outage_event_id, raw_document_id)
    )
    """,
    """
    CREATE UNIQUE INDEX event_source_one_primary ON event_source (outage_event_id)
      WHERE role = 'primary'
    """,
    "CREATE INDEX event_source_doc_idx ON event_source (raw_document_id)",
    """
    ALTER TABLE review_item ADD CONSTRAINT review_item_outage_event_fk
      FOREIGN KEY (outage_event_id) REFERENCES outage_event(id)
    """,
    """
    CREATE FUNCTION outage_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      RAISE EXCEPTION '% on % is not allowed, the table is append-only', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'RW011';
    END
    $$
    """,
    """
    CREATE TRIGGER outage_event_append_only BEFORE UPDATE OR DELETE ON outage_event
      FOR EACH ROW EXECUTE FUNCTION outage_append_only()
    """,
    """
    CREATE TRIGGER outage_event_no_truncate BEFORE TRUNCATE ON outage_event
      FOR EACH STATEMENT EXECUTE FUNCTION outage_append_only()
    """,
    """
    CREATE TRIGGER event_source_append_only BEFORE UPDATE OR DELETE ON event_source
      FOR EACH ROW EXECUTE FUNCTION outage_append_only()
    """,
    """
    CREATE TRIGGER event_source_no_truncate BEFORE TRUNCATE ON event_source
      FOR EACH STATEMENT EXECUTE FUNCTION outage_append_only()
    """,
    """
    CREATE FUNCTION event_source_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
      event_known timestamptz;
    BEGIN
      IF NEW.known_at IS NULL THEN
        NEW.known_at := clock_timestamp();
      END IF;
      SELECT known_at INTO event_known FROM outage_event WHERE id = NEW.outage_event_id;
      IF event_known IS NOT NULL AND NEW.known_at < event_known THEN
        RAISE EXCEPTION 'event_source.known_at % is before outage_event.known_at %',
          NEW.known_at, event_known USING ERRCODE = 'RW002';
      END IF;
      RETURN NEW;
    END
    $$
    """,
    """
    CREATE TRIGGER event_source_before_insert BEFORE INSERT ON event_source
      FOR EACH ROW EXECUTE FUNCTION event_source_before_insert()
    """,
    """
    CREATE FUNCTION outage_event_has_primary_source() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM event_source s
                     WHERE s.outage_event_id = NEW.id AND s.role = 'primary') THEN
        RAISE EXCEPTION 'outage_event % has no primary source', NEW.id USING ERRCODE = 'RW012';
      END IF;
      RETURN NULL;
    END
    $$
    """,
    """
    CREATE CONSTRAINT TRIGGER outage_event_primary_source AFTER INSERT ON outage_event
      DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION outage_event_has_primary_source()
    """,
]

FUNCTIONS = "outage_append_only(), event_source_before_insert(), outage_event_has_primary_source()"


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)
    op.execute(f"REVOKE ALL ON {TABLES} FROM PUBLIC")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {FUNCTIONS} FROM PUBLIC")
    op.execute(grant("SELECT, INSERT", TABLES))
    op.execute(revoke("UPDATE, DELETE, TRUNCATE", TABLES))


def downgrade() -> None:
    raise NotImplementedError("forward-only")
