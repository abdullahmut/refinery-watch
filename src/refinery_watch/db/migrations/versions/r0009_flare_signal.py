"""Flare signal: footprints, detections, coverage, scored days, thresholds and alerts."""

from alembic import op

from refinery_watch.db.migrations.params import grant, revoke

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TABLE_NAMES = (
    "plant_footprint",
    "flare_detection",
    "flare_coverage",
    "flare_day",
    "flare_threshold",
    "flare_alert",
    "flare_alert_attempt",
)
TABLES = ", ".join(TABLE_NAMES)

TABLE_STATEMENTS = [
    """
    CREATE TABLE plant_footprint (
      id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      refinery_id          text NOT NULL REFERENCES refinery(id),
      supersedes           bigint UNIQUE REFERENCES plant_footprint(id),
      kind                 text NOT NULL CHECK (kind IN ('osm_polygon','osm_bounds',
                             'point_radius')),
      approximate          boolean GENERATED ALWAYS AS (kind <> 'osm_polygon') STORED,
      center_lat           numeric(8,5) NOT NULL CHECK (center_lat BETWEEN -90 AND 90),
      center_lon           numeric(8,5) NOT NULL CHECK (center_lon BETWEEN -180 AND 180),
      radius_m             integer CHECK (radius_m BETWEEN 500 AND 10000),
      osm_refs             text[] NOT NULL DEFAULT '{}',
      rings                jsonb,
      vertex_count         integer NOT NULL CHECK (vertex_count BETWEEN 0 AND 5000),
      west                 numeric(8,5) NOT NULL,
      south                numeric(8,5) NOT NULL,
      east                 numeric(8,5) NOT NULL,
      north                numeric(8,5) NOT NULL,
      geometry_sha256      text NOT NULL CHECK (geometry_sha256 ~ '^[0-9a-f]{64}$'),
      location_verified    boolean NOT NULL,
      location_source_url  text,
      raw_document_id      bigint REFERENCES raw_document(id),
      known_at             timestamptz NOT NULL,
      CHECK ((kind = 'point_radius') = (rings IS NULL)),
      CHECK ((kind = 'point_radius') = (radius_m IS NOT NULL)),
      CHECK (west < east AND south < north)
    )
    """,
    """
    CREATE UNIQUE INDEX plant_footprint_one_root ON plant_footprint (refinery_id)
      WHERE supersedes IS NULL
    """,
    """
    CREATE TABLE flare_detection (
      id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      refinery_id      text NOT NULL REFERENCES refinery(id),
      footprint_id     bigint NOT NULL REFERENCES plant_footprint(id),
      satellite        text NOT NULL CHECK (satellite IN ('SNPP','NOAA20')),
      product          text NOT NULL CHECK (product IN ('NRT','SP')),
      acquired_at      timestamptz NOT NULL,
      latitude         numeric(8,5) NOT NULL,
      longitude        numeric(8,5) NOT NULL,
      frp_mw           numeric(9,2) NOT NULL CHECK (frp_mw >= 0),
      confidence       text NOT NULL CHECK (confidence IN ('l','n','h')),
      daynight         text NOT NULL CHECK (daynight IN ('D','N')),
      raw_document_id  bigint NOT NULL REFERENCES raw_document(id),
      known_at         timestamptz NOT NULL,
      UNIQUE (refinery_id, satellite, product, acquired_at, latitude, longitude)
    )
    """,
    """
    CREATE INDEX flare_detection_day_idx ON flare_detection
      (refinery_id, ((acquired_at AT TIME ZONE 'UTC')::date))
    """,
    """
    CREATE TABLE flare_coverage (
      id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      refinery_id      text NOT NULL REFERENCES refinery(id),
      day              date NOT NULL,
      satellite        text NOT NULL CHECK (satellite IN ('SNPP','NOAA20')),
      product          text NOT NULL CHECK (product IN ('NRT','SP')),
      raw_document_id  bigint NOT NULL REFERENCES raw_document(id),
      known_at         timestamptz NOT NULL
    )
    """,
    """
    CREATE INDEX flare_coverage_idx ON flare_coverage (refinery_id, day, satellite, product)
    """,
    """
    CREATE TABLE flare_day (
      id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      refinery_id      text NOT NULL REFERENCES refinery(id),
      footprint_id     bigint NOT NULL REFERENCES plant_footprint(id),
      day              date NOT NULL,
      supersedes       bigint UNIQUE REFERENCES flare_day(id),
      n_detections     integer NOT NULL CHECK (n_detections >= 0),
      frp_sum_mw       numeric(10,2) NOT NULL CHECK (frp_sum_mw >= 0),
      products         text[] NOT NULL,
      x                double precision NOT NULL,
      baseline_median  double precision,
      baseline_mad     double precision,
      baseline_days    smallint NOT NULL CHECK (baseline_days BETWEEN 0 AND 90),
      z                double precision,
      backfilled       boolean NOT NULL,
      available_at     timestamptz NOT NULL,
      job_run_id       bigint REFERENCES job_run(id),
      known_at         timestamptz NOT NULL,
      CHECK ((z IS NULL) = (baseline_days < 60)),
      CHECK (backfilled OR available_at = known_at),
      CHECK (available_at <= known_at)
    )
    """,
    """
    CREATE UNIQUE INDEX flare_day_one_root ON flare_day (refinery_id, day)
      WHERE supersedes IS NULL
    """,
    "CREATE INDEX flare_day_lookup_idx ON flare_day (refinery_id, day, known_at)",
    """
    CREATE TABLE flare_threshold (
      id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      kind            text NOT NULL CHECK (kind IN ('signal','alert')),
      z_min           numeric(5,2) NOT NULL CHECK (z_min > 0),
      min_detections  smallint NOT NULL CHECK (min_detections >= 1),
      method          text NOT NULL CHECK (method IN ('default','calibrated')),
      params          jsonb NOT NULL DEFAULT '{}',
      known_at        timestamptz NOT NULL
    )
    """,
    """
    CREATE TABLE flare_alert (
      id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      refinery_id       text NOT NULL REFERENCES refinery(id),
      flare_day_id      bigint NOT NULL REFERENCES flare_day(id),
      flare_date        date NOT NULL,
      built_at          timestamptz NOT NULL,
      alert_day         date GENERATED ALWAYS AS
                          ((built_at AT TIME ZONE 'Asia/Dubai')::date) STORED,
      figures           jsonb NOT NULL,
      evidence_found    boolean NOT NULL,
      subject           text NOT NULL CHECK (subject !~ '[\\r\\n]'),
      body_text         text NOT NULL,
      body_html         text NOT NULL,
      send_fingerprint  text NOT NULL CHECK (send_fingerprint ~ '^[0-9a-f]{64}$'),
      idempotency_key   text NOT NULL UNIQUE CHECK (idempotency_key ~ '^[0-9a-f]{32}$'),
      UNIQUE (refinery_id, alert_day),
      UNIQUE (refinery_id, flare_date)
    )
    """,
    """
    CREATE TABLE flare_alert_attempt (
      id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      flare_alert_id       bigint NOT NULL REFERENCES flare_alert(id),
      attempt              smallint NOT NULL CHECK (attempt BETWEEN 1 AND 3),
      attempted_at         timestamptz NOT NULL,
      status               text NOT NULL CHECK (status IN ('sent','send_failed')),
      http_status          smallint,
      provider_message_id  text,
      error                text,
      UNIQUE (flare_alert_id, attempt),
      CHECK ((status = 'sent') = (provider_message_id IS NOT NULL))
    )
    """,
    """
    CREATE UNIQUE INDEX flare_alert_one_sent ON flare_alert_attempt (flare_alert_id)
      WHERE status = 'sent'
    """,
]

# The defaults are code constants, not knowledge, so they are in force at every time.
# They go in before the stamp trigger exists, which would replace -infinity.
THRESHOLD_SEED = """
INSERT INTO flare_threshold (kind, z_min, min_detections, method, known_at)
VALUES ('signal', 3.50, 1, 'default', '-infinity'),
       ('alert', 6.00, 2, 'default', '-infinity')
"""

TRIGGER_FUNCTIONS = [
    """
    CREATE FUNCTION flare_stamp_known_at() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      NEW.known_at := clock_timestamp();
      RETURN NEW;
    END
    $$
    """,
    """
    CREATE FUNCTION plant_footprint_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
      prior plant_footprint%ROWTYPE;
    BEGIN
      NEW.known_at := clock_timestamp();
      IF NEW.supersedes IS NOT NULL THEN
        SELECT * INTO prior FROM plant_footprint WHERE id = NEW.supersedes;
        IF NOT FOUND OR prior.refinery_id <> NEW.refinery_id
           OR EXISTS (SELECT 1 FROM plant_footprint s WHERE s.supersedes = prior.id) THEN
          RAISE EXCEPTION 'plant_footprint % cannot be superseded by this row', NEW.supersedes
            USING ERRCODE = 'RW021';
        END IF;
        IF NEW.known_at <= prior.known_at THEN
          RAISE EXCEPTION 'known_at is not later than the row it supersedes'
            USING ERRCODE = 'RW022';
        END IF;
      END IF;
      RETURN NEW;
    END
    $$
    """,
    """
    CREATE FUNCTION flare_day_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
      prior plant_footprint%ROWTYPE;
      older flare_day%ROWTYPE;
    BEGIN
      NEW.known_at := clock_timestamp();
      IF NOT NEW.backfilled AND NEW.available_at IS NULL THEN
        NEW.available_at := NEW.known_at;
      END IF;
      SELECT * INTO prior FROM plant_footprint WHERE id = NEW.footprint_id;
      IF NOT FOUND OR prior.refinery_id <> NEW.refinery_id THEN
        RAISE EXCEPTION 'footprint % does not belong to %', NEW.footprint_id, NEW.refinery_id
          USING ERRCODE = 'RW023';
      END IF;
      IF NEW.supersedes IS NOT NULL THEN
        SELECT * INTO older FROM flare_day WHERE id = NEW.supersedes;
        IF NOT FOUND OR older.refinery_id <> NEW.refinery_id OR older.day <> NEW.day
           OR EXISTS (SELECT 1 FROM flare_day s WHERE s.supersedes = older.id) THEN
          RAISE EXCEPTION 'flare_day % cannot be superseded by this row', NEW.supersedes
            USING ERRCODE = 'RW021';
        END IF;
        IF NEW.known_at <= older.known_at THEN
          RAISE EXCEPTION 'known_at is not later than the row it supersedes'
            USING ERRCODE = 'RW022';
        END IF;
      END IF;
      RETURN NEW;
    END
    $$
    """,
    """
    CREATE FUNCTION flare_alert_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
      d flare_day%ROWTYPE;
    BEGIN
      NEW.built_at := clock_timestamp();
      SELECT * INTO d FROM flare_day WHERE id = NEW.flare_day_id;
      IF NOT FOUND OR d.backfilled OR d.day <> NEW.flare_date
         OR d.refinery_id <> NEW.refinery_id
         OR EXISTS (SELECT 1 FROM flare_day s WHERE s.supersedes = d.id) THEN
        RAISE EXCEPTION 'flare_day % is not a current live day for this alert', NEW.flare_day_id
          USING ERRCODE = 'RW024';
      END IF;
      RETURN NEW;
    END
    $$
    """,
    """
    CREATE FUNCTION flare_alert_attempt_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
      built timestamptz;
    BEGIN
      NEW.attempted_at := clock_timestamp();
      SELECT built_at INTO built FROM flare_alert WHERE id = NEW.flare_alert_id;
      IF EXISTS (SELECT 1 FROM flare_alert_attempt a
                 WHERE a.flare_alert_id = NEW.flare_alert_id AND a.status = 'sent')
         OR NEW.attempted_at > built + interval '18 hours' THEN
        RAISE EXCEPTION 'alert % takes no more attempts', NEW.flare_alert_id
          USING ERRCODE = 'RW025';
      END IF;
      RETURN NEW;
    END
    $$
    """,
]

INSERT_TRIGGERS = {
    "plant_footprint": "plant_footprint_before_insert",
    "flare_detection": "flare_stamp_known_at",
    "flare_coverage": "flare_stamp_known_at",
    "flare_day": "flare_day_before_insert",
    "flare_threshold": "flare_stamp_known_at",
    "flare_alert": "flare_alert_before_insert",
    "flare_alert_attempt": "flare_alert_attempt_before_insert",
}

CURRENT_FUNCTIONS = [
    """
    CREATE FUNCTION footprint_current_as_of(as_of timestamptz)
    RETURNS SETOF plant_footprint LANGUAGE sql STABLE AS $$
      SELECT f.* FROM plant_footprint f
      WHERE f.known_at <= as_of
        AND NOT EXISTS (SELECT 1 FROM plant_footprint s
                        WHERE s.supersedes = f.id AND s.known_at <= as_of)
    $$
    """,
    """
    CREATE FUNCTION flare_day_current_as_of(as_of timestamptz)
    RETURNS SETOF flare_day LANGUAGE sql STABLE AS $$
      SELECT d.* FROM flare_day d
      WHERE d.known_at <= as_of
        AND NOT EXISTS (SELECT 1 FROM flare_day s
                        WHERE s.supersedes = d.id AND s.known_at <= as_of)
    $$
    """,
    """
    CREATE FUNCTION flare_threshold_as_of(p_kind text, as_of timestamptz)
    RETURNS flare_threshold LANGUAGE sql STABLE AS $$
      SELECT t.* FROM flare_threshold t
      WHERE t.kind = p_kind AND t.known_at <= as_of
      ORDER BY t.known_at DESC, t.id DESC
      LIMIT 1
    $$
    """,
]

SCORE_FUNCTIONS = [
    """
    CREATE FUNCTION flare_day_figures(p_refinery_id text, p_day date, as_of timestamptz)
    RETURNS TABLE (n_detections int, frp_sum_mw numeric, products text[],
                   last_acquired_at timestamptz)
    LANGUAGE sql STABLE AS $$
      WITH cov AS (
        SELECT c.satellite, bool_or(c.product = 'SP') AS has_sp
        FROM flare_coverage c
        WHERE c.refinery_id = p_refinery_id AND c.day = p_day AND c.known_at <= as_of
        GROUP BY c.satellite
      ), chosen AS (
        SELECT satellite, CASE WHEN has_sp THEN 'SP' ELSE 'NRT' END AS product FROM cov
      ), det AS (
        SELECT d.frp_mw, d.acquired_at
        FROM chosen c
        JOIN flare_detection d
          ON d.refinery_id = p_refinery_id AND d.satellite = c.satellite
         AND d.product = c.product AND d.known_at <= as_of
         AND (d.acquired_at AT TIME ZONE 'UTC')::date = p_day
      )
      SELECT (SELECT count(*) FROM det)::int,
             (SELECT coalesce(sum(frp_mw), 0) FROM det),
             (SELECT array_agg(satellite || ':' || product ORDER BY satellite) FROM chosen),
             (SELECT max(acquired_at) FROM det)
      WHERE (SELECT count(*) FROM chosen) = 2
    $$
    """,
    """
    CREATE FUNCTION flare_baseline(p_refinery_id text, p_day date, as_of timestamptz)
    RETURNS TABLE (median_x double precision, mad_x double precision, n_days int)
    LANGUAGE sql STABLE AS $$
      WITH w AS (
        SELECT d.x FROM flare_day_current_as_of(as_of) d
        WHERE d.refinery_id = p_refinery_id AND d.day BETWEEN p_day - 90 AND p_day - 1
      ), m AS (
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY x) AS med, count(*)::int AS n FROM w
      )
      SELECT m.med,
             (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(w.x - m.med)) FROM w),
             m.n
      FROM m
    $$
    """,
    """
    CREATE FUNCTION flare_day_write(p_refinery_id text, p_day date, p_backfilled boolean,
                                    p_job_run_id bigint)
    RETURNS bigint LANGUAGE plpgsql AS $$
    DECLARE
      f        record;
      b        record;
      cur      flare_day%ROWTYPE;
      fp       bigint;
      xv       double precision;
      zv       double precision;
      avail    timestamptz;
      new_id   bigint;
    BEGIN
      PERFORM pg_advisory_xact_lock(7746022);
      SELECT * INTO f FROM flare_day_figures(p_refinery_id, p_day, clock_timestamp());
      IF NOT FOUND THEN
        RETURN NULL;
      END IF;
      SELECT * INTO cur FROM flare_day_current_as_of(clock_timestamp())
        WHERE refinery_id = p_refinery_id AND day = p_day;
      IF FOUND AND cur.n_detections = f.n_detections AND cur.frp_sum_mw = f.frp_sum_mw
         AND cur.products = f.products THEN
        RETURN NULL;
      END IF;
      SELECT id INTO fp FROM footprint_current_as_of(clock_timestamp())
        WHERE refinery_id = p_refinery_id;
      IF fp IS NULL THEN
        RETURN NULL;
      END IF;
      SELECT * INTO b FROM flare_baseline(p_refinery_id, p_day, clock_timestamp());
      xv := ln(1 + f.frp_sum_mw);
      IF b.n_days >= 60 THEN
        zv := (xv - b.median_x) / greatest(1.4826 * b.mad_x, 0.5);
      END IF;
      IF p_backfilled THEN
        avail := least(clock_timestamp(),
                       coalesce(f.last_acquired_at,
                                (p_day + 1)::timestamp AT TIME ZONE 'UTC') + interval '3 hours');
      END IF;
      INSERT INTO flare_day (refinery_id, footprint_id, day, supersedes, n_detections,
                             frp_sum_mw, products, x, baseline_median, baseline_mad,
                             baseline_days, z, backfilled, available_at, job_run_id)
      VALUES (p_refinery_id, fp, p_day, cur.id, f.n_detections, f.frp_sum_mw, f.products, xv,
              b.median_x, b.mad_x, b.n_days, zv, p_backfilled, avail, p_job_run_id)
      RETURNING id INTO new_id;
      RETURN new_id;
    END
    $$
    """,
]

EVIDENCE_FUNCTIONS = [
    """
    CREATE FUNCTION outage_confidence_as_of(as_of timestamptz)
    RETURNS TABLE (outage_event_id bigint, chain_id bigint, stored text, effective text,
                   flare_day_id bigint)
    LANGUAGE sql STABLE AS $$
      WITH flagged AS (
        SELECT d.id, d.refinery_id, d.day
        FROM flare_day_current_as_of(as_of) d
        JOIN flare_threshold_as_of('signal', as_of) t ON t.id IS NOT NULL
        WHERE d.z IS NOT NULL AND d.z >= t.z_min AND d.n_detections >= t.min_detections
      )
      SELECT o.id, o.chain_id, o.confidence,
             CASE WHEN f.id IS NOT NULL THEN 'medium' ELSE o.confidence END,
             f.id
      FROM outage_current_as_of(as_of) o
      LEFT JOIN LATERAL (
        SELECT fl.id FROM flagged fl
        WHERE o.confidence = 'low'
          AND o.event_type IN ('unplanned', 'planned')
          AND o.match_score >= 90
          AND (o.refinery_unit_id IS NOT NULL OR o.whole_plant)
          AND o.capacity_basis <> 'not_stated'
          AND NOT o.start_unknown AND NOT o.old_start
          AND fl.refinery_id = o.refinery_id
          AND fl.day BETWEEN o.start_date - 3 AND o.start_date + 3
          AND EXISTS (SELECT 1 FROM event_source s
                      JOIN outage_event e ON e.id = s.outage_event_id
                      WHERE e.chain_id = o.chain_id AND s.known_at <= as_of
                        AND s.source_kind <> 'regulator')
        ORDER BY abs(fl.day - o.start_date), fl.day
        LIMIT 1
      ) f ON true
    $$
    """,
    """
    CREATE FUNCTION flare_evidence_as_of(p_refinery_id text, p_day date, as_of timestamptz)
    RETURNS TABLE (kind text, ref_id bigint, item_date date, event_type text, unit_label text,
                   confidence text)
    LANGUAGE sql STABLE AS $$
      WITH eff AS (
        SELECT c.outage_event_id, c.effective FROM outage_confidence_as_of(as_of) c
      ), items AS (
        SELECT 'outage_near'::text AS kind, o.id AS ref_id, o.start_date AS item_date,
               o.event_type, o.unit_label, e.effective AS confidence
        FROM outage_current_as_of(as_of) o JOIN eff e ON e.outage_event_id = o.id
        WHERE o.refinery_id = p_refinery_id
          AND o.start_date BETWEEN p_day - 3 AND p_day + 3
        UNION ALL
        SELECT 'outage_open', o.id, o.start_date, o.event_type, o.unit_label, e.effective
        FROM outage_as_of(as_of) o JOIN eff e ON e.outage_event_id = o.id
        WHERE o.refinery_id = p_refinery_id
          AND o.start_date NOT BETWEEN p_day - 3 AND p_day + 3
        UNION ALL
        SELECT 'regulator_near', t.id,
               (coalesce(t.started_at, t.known_at) AT TIME ZONE 'UTC')::date,
               t.event_kind, t.units_text, NULL::text
        FROM tceq_event t
        WHERE t.refinery_id = p_refinery_id AND t.known_at <= as_of
          AND (coalesce(t.started_at, t.known_at) AT TIME ZONE 'UTC')::date
              BETWEEN p_day - 3 AND p_day + 3
      )
      SELECT i.kind, i.ref_id, i.item_date, i.event_type, i.unit_label, i.confidence
      FROM items i
      ORDER BY CASE i.kind WHEN 'outage_near' THEN 1 WHEN 'outage_open' THEN 2 ELSE 3 END,
               i.item_date, i.ref_id
    $$
    """,
]

TRIGGER_FUNCTION_NAMES = (
    "flare_stamp_known_at(), plant_footprint_before_insert(), flare_day_before_insert(),"
    " flare_alert_before_insert(), flare_alert_attempt_before_insert()"
)
SQL_FUNCTION_NAMES = (
    "footprint_current_as_of(timestamptz), flare_day_current_as_of(timestamptz),"
    " flare_threshold_as_of(text, timestamptz), flare_day_figures(text, date, timestamptz),"
    " flare_baseline(text, date, timestamptz), flare_day_write(text, date, boolean, bigint),"
    " outage_confidence_as_of(timestamptz), flare_evidence_as_of(text, date, timestamptz)"
)


def _create_triggers() -> None:
    for table, function in INSERT_TRIGGERS.items():
        op.execute(
            f"CREATE TRIGGER {table}_before_insert BEFORE INSERT ON {table}"
            f" FOR EACH ROW EXECUTE FUNCTION {function}()"
        )
    for table in TABLE_NAMES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}"
            " FOR EACH ROW EXECUTE FUNCTION outage_append_only()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table}"
            " FOR EACH STATEMENT EXECUTE FUNCTION outage_append_only()"
        )


def upgrade() -> None:
    for statement in TABLE_STATEMENTS:
        op.execute(statement)
    op.execute(THRESHOLD_SEED)
    for statement in TRIGGER_FUNCTIONS:
        op.execute(statement)
    _create_triggers()
    for statement in CURRENT_FUNCTIONS + SCORE_FUNCTIONS + EVIDENCE_FUNCTIONS:
        op.execute(statement)
    op.execute(f"REVOKE ALL ON {TABLES} FROM PUBLIC")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {TRIGGER_FUNCTION_NAMES} FROM PUBLIC")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {SQL_FUNCTION_NAMES} FROM PUBLIC")
    op.execute(grant("SELECT, INSERT", TABLES))
    op.execute(revoke("UPDATE, DELETE, TRUNCATE", TABLES))
    op.execute(grant("EXECUTE", f"FUNCTION {SQL_FUNCTION_NAMES}"))


def downgrade() -> None:
    raise NotImplementedError("forward-only")
