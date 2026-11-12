# 037: Plant footprints from OpenStreetMap, with a flagged fallback

Date: 2026-11-12

## Context

Detections must be counted inside each refinery. The `refinery` table holds no coordinates, and
`refineries.csv` has no latitude or longitude column. OpenStreetMap maps many refineries as
polygons tagged `industrial=refinery`, but not all, and a name search alone can pick the wrong
site.

## Options

- Add latitude and longitude to `refinery` and its CSV. That changes the reference loader and the
  schema branch for a column only this feature needs.
- Find plants in OSM by name. Names repeat and are spelled many ways, so a wrong polygon is likely.
- An operator file of one point per plant, `refinery_locations.csv`, then an Overpass query around
  that point. Use the OSM polygons that contain the point. If none does, use the point and a radius,
  and flag the footprint as approximate. The file may name an OSM way or relation to force a choice.

## Decision

The third. The locations file has `refinery_id, lat, lon, radius_m, osm_ref, source_url, verified`.
Its path comes from `REFINERY_LOCATIONS_CSV_PATH`, like the refineries file. Kinds are
`osm_polygon`, `osm_bounds` (a relation whose outer ring could not be closed, so its bounding box is
used) and `point_radius`. Only `osm_polygon` is exact. A detection counts if it is inside the
footprint or within 375 m of its edge, one VIIRS pixel, because detection positions are pixel
centres. Point-in-polygon and distance are about 60 lines of plain Python, so there is no GEOS,
shapely or PostGIS.

## Consequences

The schema branch and the reference loader are not touched. Someone has to compile one point per
plant by hand. Approximate footprints are visible in every alert and in the research breakdown.
Holes in OSM multipolygons are ignored, and two adjacent plants can both count one detection. OSM
data is ODbL: any display of the polygons must credit OpenStreetMap contributors. To undo, write
new footprint rows that supersede the old ones.
