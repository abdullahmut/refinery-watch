"""Hit rate, false alarm rate and lead time of the flare signal. Every figure is a SQL column."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final, Literal

from refinery_watch.db.connect import Conn
from refinery_watch.db.events import require_aware

NO_HISTORY: Final = (
    "No FIRMS history is loaded. No hit rate, false alarm rate or lead time is reported."
)
WINDOW_DAYS: Final = 3
QUIET_DAYS: Final = 7
DAYS_PER_MONTH: Final = "30.4375"


@dataclass(frozen=True, slots=True)
class GroupFigures:
    key: str
    scored_plant_days: int
    unplanned_events: int
    hits: int
    hit_rate: Decimal | None
    false_alarm_days: int
    quiet_plant_days: int
    false_alarms_per_plant_month: Decimal | None


@dataclass(frozen=True, slots=True)
class ResearchFigures:
    as_of: datetime
    kind: str
    z_min: Decimal
    threshold_method: str
    scored_plant_days: int
    plants: int
    first_day: date | None
    last_day: date | None
    unplanned_events: int
    planned_events: int
    hits: int
    hit_rate: Decimal | None
    hit_rate_low: Decimal | None
    hit_rate_high: Decimal | None  # Wilson 95 percent
    false_alarm_days: int
    quiet_plant_months: Decimal | None
    false_alarms_per_plant_month: Decimal | None
    median_lead_hours: Decimal | None
    lead_n: int
    flare_first_n: int
    by_region: tuple[GroupFigures, ...]
    by_footprint_kind: tuple[GroupFigures, ...]
    backfilled_share: Decimal | None


CTES: Final = """
WITH days AS MATERIALIZED (
  SELECT d.refinery_id, d.day, d.backfilled, d.available_at, r.region, pf.kind AS fkind,
         (d.z >= %(z_min)s AND d.n_detections >= %(min_detections)s) AS is_flagged
  FROM flare_day_current_as_of(%(as_of)s) d
  JOIN refinery r ON r.id = d.refinery_id
  JOIN plant_footprint pf ON pf.id = d.footprint_id
  WHERE d.z IS NOT NULL
), outages AS MATERIALIZED (
  SELECT id, chain_id, refinery_id, event_type, start_date FROM outage_current_as_of(%(as_of)s)
), regulator AS MATERIALIZED (
  SELECT refinery_id, (coalesce(started_at, known_at) AT TIME ZONE 'UTC')::date AS item_date
  FROM tceq_event WHERE refinery_id IS NOT NULL AND known_at <= %(as_of)s
), quiet AS MATERIALIZED (
  SELECT d.* FROM days d
  WHERE NOT EXISTS (SELECT 1 FROM outages o WHERE o.refinery_id = d.refinery_id
                    AND o.start_date BETWEEN d.day - @QUIET@ AND d.day + @QUIET@)
    AND NOT EXISTS (SELECT 1 FROM regulator t WHERE t.refinery_id = d.refinery_id
                    AND t.item_date BETWEEN d.day - @QUIET@ AND d.day + @QUIET@)
), events AS MATERIALIZED (
  SELECT o.id, o.chain_id, o.refinery_id, o.event_type, o.start_date, r.region, fp.kind AS fkind
  FROM outages o
  JOIN refinery r ON r.id = o.refinery_id
  JOIN footprint_current_as_of(%(as_of)s) fp ON fp.refinery_id = o.refinery_id
  WHERE o.event_type IN ('unplanned', 'planned')
    AND EXISTS (SELECT 1 FROM days d WHERE d.refinery_id = o.refinery_id AND d.day = o.start_date)
), event_stats AS MATERIALIZED (
  SELECT e.*,
         (SELECT min(coalesce(rd.published_at, rd.fetched_at))
          FROM event_source s
          JOIN outage_event oe ON oe.id = s.outage_event_id
          JOIN raw_document rd ON rd.id = s.raw_document_id
          WHERE oe.chain_id = e.chain_id AND s.known_at <= %(as_of)s) AS first_report,
         (SELECT min(f.available_at) FROM days f
          WHERE f.is_flagged AND f.refinery_id = e.refinery_id
            AND f.day BETWEEN e.start_date - @WINDOW@ AND e.start_date + @WINDOW@
         ) AS first_flag
  FROM events e
), leads AS MATERIALIZED (
  SELECT extract(epoch FROM (first_report - first_flag)) / 3600.0 AS hours
  FROM event_stats WHERE event_type = 'unplanned' AND first_flag IS NOT NULL
    AND first_report IS NOT NULL
)
""".replace("@QUIET@", str(QUIET_DAYS)).replace("@WINDOW@", str(WINDOW_DAYS))

OVERALL_BODY: Final = """
, agg AS (
  SELECT (SELECT count(*) FROM days) AS scored_plant_days,
         (SELECT count(DISTINCT refinery_id) FROM days) AS plants,
         (SELECT min(day) FROM days) AS first_day,
         (SELECT max(day) FROM days) AS last_day,
         (SELECT count(*) FROM event_stats WHERE event_type = 'unplanned') AS unplanned_events,
         (SELECT count(*) FROM event_stats WHERE event_type = 'planned') AS planned_events,
         (SELECT count(*) FROM event_stats
          WHERE event_type = 'unplanned' AND first_flag IS NOT NULL) AS hits,
         (SELECT count(*) FROM quiet WHERE is_flagged) AS false_alarm_days,
         (SELECT count(*) FROM quiet) AS quiet_plant_days,
         (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY hours) FROM leads) AS median_lead,
         (SELECT count(*) FROM leads) AS lead_n,
         (SELECT count(*) FROM leads WHERE hours > 0) AS flare_first_n,
         (SELECT count(*) FILTER (WHERE backfilled) FROM days) AS backfilled_days
), wilson AS (
  SELECT agg.*,
         hits::float8 / nullif(unplanned_events, 0) AS p,
         nullif(unplanned_events, 0)::float8 AS n,
         1.96::float8 AS z
  FROM agg
)
SELECT scored_plant_days, plants, first_day, last_day, unplanned_events, planned_events, hits,
       round(p::numeric, 4) AS hit_rate,
       round(((p + z * z / (2 * n) - z * sqrt(p * (1 - p) / n + z * z / (4.0 * n * n)))
              / (1 + z * z / n))::numeric, 4) AS hit_rate_low,
       round(((p + z * z / (2 * n) + z * sqrt(p * (1 - p) / n + z * z / (4.0 * n * n)))
              / (1 + z * z / n))::numeric, 4) AS hit_rate_high,
       false_alarm_days,
       CASE WHEN quiet_plant_days > 0
            THEN round(quiet_plant_days / @MONTH@::numeric, 2) END AS quiet_plant_months,
       CASE WHEN quiet_plant_days > 0
            THEN round(false_alarm_days * @MONTH@::numeric / quiet_plant_days, 4)
       END AS false_alarms_per_plant_month,
       round(median_lead::numeric, 1) AS median_lead_hours, lead_n, flare_first_n,
       CASE WHEN scored_plant_days > 0
            THEN round(backfilled_days::numeric / scored_plant_days, 4) END AS backfilled_share
FROM wilson
""".replace("@MONTH@", DAYS_PER_MONTH)
OVERALL_SQL: Final = CTES + OVERALL_BODY

GROUP_COLUMNS: Final = {"region": "region", "fkind": "fkind"}


GROUP_BODY: Final = """
SELECT t.*,
       round(t.hits::numeric / nullif(t.unplanned_events, 0), 4) AS hit_rate,
       round(t.false_alarm_days * @MONTH@::numeric / nullif(t.quiet_plant_days, 0), 4)
         AS per_plant_month
FROM (
  SELECT g.key,
         (SELECT count(*) FROM days WHERE @COL@ = g.key) AS scored_plant_days,
         (SELECT count(*) FROM event_stats
          WHERE @COL@ = g.key AND event_type = 'unplanned') AS unplanned_events,
         (SELECT count(*) FROM event_stats
          WHERE @COL@ = g.key AND event_type = 'unplanned' AND first_flag IS NOT NULL) AS hits,
         (SELECT count(*) FROM quiet WHERE @COL@ = g.key AND is_flagged) AS false_alarm_days,
         (SELECT count(*) FROM quiet WHERE @COL@ = g.key) AS quiet_plant_days
  FROM (SELECT @COL@ AS key FROM days UNION SELECT @COL@ FROM event_stats) g
) t
ORDER BY t.key
""".replace("@MONTH@", DAYS_PER_MONTH)


def _group_sql(column: str) -> str:
    return (CTES + GROUP_BODY).replace("@COL@", column)


def _groups(conn: Conn, column: str, params: dict[str, Any]) -> tuple[GroupFigures, ...]:
    rows = conn.execute(_group_sql(GROUP_COLUMNS[column]), params).fetchall()
    return tuple(
        GroupFigures(
            key=r["key"],
            scored_plant_days=int(r["scored_plant_days"]),
            unplanned_events=int(r["unplanned_events"]),
            hits=int(r["hits"]),
            hit_rate=r["hit_rate"],
            false_alarm_days=int(r["false_alarm_days"]),
            quiet_plant_days=int(r["quiet_plant_days"]),
            false_alarms_per_plant_month=r["per_plant_month"],
        )
        for r in rows
    )


def figures(conn: Conn, as_of: datetime, kind: Literal["signal", "alert"]) -> ResearchFigures:
    """All figures as known at `as_of`, for the threshold in force then."""
    require_aware(as_of, "as_of")
    threshold = conn.execute(
        "SELECT z_min, min_detections, method FROM flare_threshold_as_of(%s, %s)"
        " WHERE id IS NOT NULL",
        (kind, as_of),
    ).fetchone()
    if threshold is None:
        raise RuntimeError(f"no {kind} threshold is in force")
    params: dict[str, Any] = {
        "as_of": as_of,
        "z_min": threshold["z_min"],
        "min_detections": threshold["min_detections"],
    }
    row = conn.execute(OVERALL_SQL, params).fetchone()
    if row is None:
        raise RuntimeError("query returned no row")
    return ResearchFigures(
        as_of=as_of,
        kind=kind,
        z_min=threshold["z_min"],
        threshold_method=threshold["method"],
        scored_plant_days=int(row["scored_plant_days"]),
        plants=int(row["plants"]),
        first_day=row["first_day"],
        last_day=row["last_day"],
        unplanned_events=int(row["unplanned_events"]),
        planned_events=int(row["planned_events"]),
        hits=int(row["hits"]),
        hit_rate=row["hit_rate"],
        hit_rate_low=row["hit_rate_low"],
        hit_rate_high=row["hit_rate_high"],
        false_alarm_days=int(row["false_alarm_days"]),
        quiet_plant_months=row["quiet_plant_months"],
        false_alarms_per_plant_month=row["false_alarms_per_plant_month"],
        median_lead_hours=row["median_lead_hours"],
        lead_n=int(row["lead_n"]),
        flare_first_n=int(row["flare_first_n"]),
        by_region=_groups(conn, "region", params),
        by_footprint_kind=_groups(conn, "fkind", params),
        backfilled_share=row["backfilled_share"],
    )


def _num(value: Decimal | None, places: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


def _section(f: ResearchFigures) -> list[str]:
    title = f"{f.kind.capitalize()} threshold"
    out = [
        f"## {title}",
        "",
        f"Score at or over {f.z_min:.2f} ({f.threshold_method}).",
        "",
        f"- Scored plant-days: {f.scored_plant_days} at {f.plants} plants,"
        f" {f.first_day} to {f.last_day}.",
        f"- Share of scored days that are backfilled: {_num(f.backfilled_share)}.",
        f"- Unplanned events with a scored start day: {f.unplanned_events}."
        f" Planned events, left out of the hit rate: {f.planned_events}.",
        f"- Hits: {f.hits} of {f.unplanned_events}. Hit rate {_num(f.hit_rate)},"
        f" Wilson 95 percent interval {_num(f.hit_rate_low)} to {_num(f.hit_rate_high)}.",
        f"- Lead time: median {_num(f.median_lead_hours, 1)} hours over {f.lead_n} events."
        f" The flare came first in {f.flare_first_n}. A negative lead means the news came first.",
        f"- False alarms: {f.false_alarm_days} flagged quiet plant-days over"
        f" {_num(f.quiet_plant_months, 2)} quiet plant-months, a rate of"
        f" {_num(f.false_alarms_per_plant_month)} per plant-month. This is an upper bound.",
        "",
    ]
    for label, groups in (("By region", f.by_region), ("By footprint kind", f.by_footprint_kind)):
        out += [f"### {title}, {label.lower()}", ""]
        out += ["| Group | Scored days | Events | Hits | Hit rate | False alarm days | Rate |"]
        out += ["| --- | --- | --- | --- | --- | --- | --- |"]
        for g in groups:
            out.append(
                f"| {g.key} | {g.scored_plant_days} | {g.unplanned_events} | {g.hits} |"
                f" {_num(g.hit_rate)} | {g.false_alarm_days} |"
                f" {_num(g.false_alarms_per_plant_month)} |"
            )
        out.append("")
    return out


def render_results(signal: ResearchFigures, alert: ResearchFigures) -> str:
    """Formats the figures. It computes nothing."""
    if signal.scored_plant_days == 0:
        return NO_HISTORY
    lines = [
        "# Flare signal results",
        "",
        f"Generated from SQL as of {signal.as_of.isoformat()}. Detections: NASA FIRMS VIIRS."
        " Outlines: OpenStreetMap contributors. Footprints that are not an exact outline are"
        " approximate, and the by-kind table shows how many scored days used each.",
        "",
        *_section(signal),
        *_section(alert),
    ]
    return "\n".join(lines)
