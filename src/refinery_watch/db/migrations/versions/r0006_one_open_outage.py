"""One open outage per unit: step 4 of the insert trigger."""

from alembic import op

from refinery_watch.db.migrations.params import pin_search_path

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

FUNCTION = """
CREATE OR REPLACE FUNCTION outage_event_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  max_known  timestamptz;
  s          outage_event%ROWTYPE;
  m          bigint;
  closing    outage_event%ROWTYPE;
  ended_chain bigint;
  ended_start date;
BEGIN
  -- 1: one writer at a time, READ COMMITTED so each statement sees committed rows
  IF current_setting('transaction_isolation') <> 'read committed' THEN
    RAISE EXCEPTION 'outage_event writes need READ COMMITTED' USING ERRCODE = 'RW001';
  END IF;
  PERFORM pg_advisory_xact_lock(7746001);

  -- 2: known_at comes from the database clock and is later than every existing row
  IF NEW.known_at IS NULL THEN
    NEW.known_at := clock_timestamp();
  ELSIF NEW.known_at > clock_timestamp() THEN
    RAISE EXCEPTION 'known_at % is in the future', NEW.known_at USING ERRCODE = 'RW002';
  END IF;
  SELECT max(known_at) INTO max_known FROM outage_event;
  IF max_known IS NOT NULL AND NEW.known_at <= max_known THEN
    RAISE EXCEPTION 'known_at % is not later than %', NEW.known_at, max_known
      USING ERRCODE = 'RW002';
  END IF;

  -- 3: chain_id belongs to the trigger
  IF NEW.chain_id IS NOT NULL THEN
    RAISE EXCEPTION 'chain_id is set by the database' USING ERRCODE = 'RW014';
  END IF;

  -- 3b: a restart cannot be dated after the day it became known (UTC)
  IF NEW.event_type = 'restart' AND NEW.start_date > (NEW.known_at AT TIME ZONE 'UTC')::date THEN
    RAISE EXCEPTION 'restart date % is after the day it became known', NEW.start_date
      USING ERRCODE = 'RW015';
  END IF;

  -- 4: a root outage needs a free unit
  IF NEW.supersedes IS NULL AND NEW.event_type <> 'restart' AND EXISTS (
       SELECT 1 FROM outage_as_of(NEW.known_at) o
       WHERE o.refinery_id = NEW.refinery_id
         AND o.refinery_unit_id IS NOT DISTINCT FROM NEW.refinery_unit_id) THEN
    RAISE EXCEPTION 'unit already has an open outage at %', NEW.refinery_id
      USING ERRCODE = 'RW006';
  END IF;

  IF NEW.supersedes IS NULL THEN
    -- 5: root rows
    IF NEW.event_type <> 'restart' THEN
      IF NEW.ends_event_id IS NOT NULL THEN
        RAISE EXCEPTION 'an outage row cannot end an event' USING ERRCODE = 'RW009';
      END IF;
    ELSE
      m := match_restart(NEW.refinery_id, NEW.refinery_unit_id, NEW.start_date, NEW.known_at,
                         NEW.whole_plant);
      IF m IS NULL THEN
        IF NEW.whole_plant AND EXISTS (
             SELECT 1 FROM outage_as_of(NEW.known_at) o
             WHERE o.refinery_id = NEW.refinery_id AND o.refinery_unit_id IS NOT NULL) THEN
          RAISE EXCEPTION 'whole-plant restart at % while units are open', NEW.refinery_id
            USING ERRCODE = 'RW013';
        END IF;
        RAISE EXCEPTION 'no open outage matches this restart' USING ERRCODE = 'RW005';
      END IF;
      IF NEW.ends_event_id IS NOT NULL AND NEW.ends_event_id <> m THEN
        RAISE EXCEPTION 'ends_event_id % does not match %', NEW.ends_event_id, m
          USING ERRCODE = 'RW009';
      END IF;
      NEW.ends_event_id := m;
    END IF;
    NEW.chain_id := NEW.id;
    RETURN NEW;
  END IF;

  -- 6: a revision replaces a row that is current now
  SELECT c.* INTO s FROM outage_current_as_of(NEW.known_at) c WHERE c.id = NEW.supersedes;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'row % is not current', NEW.supersedes USING ERRCODE = 'RW003';
  END IF;

  -- 7: same family, same chain
  IF (s.event_type = 'restart') <> (NEW.event_type = 'restart') THEN
    RAISE EXCEPTION 'a revision must stay in its family' USING ERRCODE = 'RW004';
  END IF;
  NEW.chain_id := s.chain_id;

  IF NEW.event_type <> 'restart' THEN
    -- 8: outage revision
    IF NEW.refinery_id <> s.refinery_id THEN
      RAISE EXCEPTION 'a revision cannot change the refinery' USING ERRCODE = 'RW007';
    END IF;
    IF NEW.ends_event_id IS NOT NULL THEN
      RAISE EXCEPTION 'an outage row cannot end an event' USING ERRCODE = 'RW009';
    END IF;
    SELECT r.* INTO closing
    FROM outage_current_as_of(NEW.known_at) r
    JOIN outage_event t ON t.id = r.ends_event_id
    WHERE r.event_type = 'restart' AND t.chain_id = s.chain_id
    LIMIT 1;
    IF FOUND THEN
      IF NEW.refinery_unit_id IS DISTINCT FROM s.refinery_unit_id
         OR NEW.whole_plant <> s.whole_plant THEN
        RAISE EXCEPTION 'a closed event cannot change unit' USING ERRCODE = 'RW007';
      END IF;
      IF NEW.start_date > closing.start_date THEN
        RAISE EXCEPTION 'start % is after the restart on %', NEW.start_date, closing.start_date
          USING ERRCODE = 'RW010';
      END IF;
    ELSIF NEW.refinery_unit_id IS DISTINCT FROM s.refinery_unit_id AND EXISTS (
            SELECT 1 FROM outage_as_of(NEW.known_at) o
            WHERE o.chain_id <> s.chain_id AND o.refinery_id = NEW.refinery_id
              AND o.refinery_unit_id IS NOT DISTINCT FROM NEW.refinery_unit_id) THEN
      RAISE EXCEPTION 'the new unit already has an open outage' USING ERRCODE = 'RW006';
    END IF;
  ELSE
    -- 9: restart revision
    IF NEW.refinery_id <> s.refinery_id
       OR NEW.refinery_unit_id IS DISTINCT FROM s.refinery_unit_id
       OR NEW.whole_plant <> s.whole_plant THEN
      RAISE EXCEPTION 'a restart revision cannot change refinery or unit' USING ERRCODE = 'RW007';
    END IF;
    IF NEW.ends_event_id IS NOT NULL AND NEW.ends_event_id <> s.ends_event_id THEN
      RAISE EXCEPTION 'ends_event_id % does not match %', NEW.ends_event_id, s.ends_event_id
        USING ERRCODE = 'RW009';
    END IF;
    NEW.ends_event_id := s.ends_event_id;
    SELECT chain_id INTO ended_chain FROM outage_event WHERE id = s.ends_event_id;
    SELECT c.start_date INTO ended_start
    FROM outage_current_as_of(NEW.known_at) c WHERE c.chain_id = ended_chain;
    IF ended_start IS NOT NULL AND NEW.start_date < ended_start THEN
      RAISE EXCEPTION 'restart % is before the event start %', NEW.start_date, ended_start
        USING ERRCODE = 'RW008';
    END IF;
  END IF;
  RETURN NEW;
END
$$
"""

PINNED = ("outage_event_before_insert()",)


def upgrade() -> None:
    op.execute(FUNCTION)
    for statement in pin_search_path(*PINNED):
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("forward-only")
