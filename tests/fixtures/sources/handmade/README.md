Hand-made samples. The content is invented and follows the documented formats. They are not real publisher output.

Every file name ends in `_handmade`. The plant is the invented "Testville" refinery, hosts are on
`example.com` and `example.org`, and no real headline or text is used.

Real fetched files are kept apart, in `hp_rss/`, `gdelt/` and `eia/` next to this folder.

The `overpass_*` and `firms_*` files are synthetic. They are invented, not fetched. The plant sits at
29.745, -95.012, and the square outline is 0.01 degrees either side of it.

The FIRMS CSV columns are not confirmed against a real reply. These files use the commonly
documented VIIRS columns: latitude, longitude, bright_ti4, scan, track, acq_date, acq_time,
satellite, instrument, confidence, version, bright_ti5, frp, daynight. The data availability layout
(`data_id,min_date,max_date`) is also assumed. Real replies go in `overpass/` and `firms/`, and the
parser tests marked for them skip until those files exist.
