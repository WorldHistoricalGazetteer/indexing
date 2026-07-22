#!/bin/sh
# tippecanoe --postfilter for the gazetteer tiles (place#131 §2).
#
# Dedupes the ';'-delimited `aat` (AAT path-segment id set) property that
# `--accumulate-attribute=aat:concat` leaves on clustered points — concat
# concatenates member strings without collapsing repeats, so a dense cluster's
# `aat` would otherwise grow with member count. Individual (unclustered)
# features already carry a per-doc-deduped `aat`; re-running dedupe on them is
# idempotent.
#
# tippecanoe invokes a postfilter as `<cmd> <layer> <zoom> <x> <y>` and streams
# the tile's features as one GeoJSON object per line on stdin, reading the
# transformed features back on stdout. We `exec jq` (which reads stdin and
# ignores the trailing positional args the shell received) so the args can't be
# misread as filenames. Keep this a fast stdin-streaming filter — no per-tile
# interpreter/import startup — since it runs for every tile at every zoom.
exec jq -c '
  if (.properties.aat | type) == "string"
  then .properties.aat |= (
    split(";") | map(select(length > 0)) | unique
    | if length == 0 then "" else ";" + join(";") + ";" end
  )
  else . end
'
