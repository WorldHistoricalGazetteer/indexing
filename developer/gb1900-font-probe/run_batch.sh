#!/bin/bash
# Phase C data-gathering: sheet-wide discovery over ~24 sheets spread across GB, chosen to maximise the
# rare font classes the premise test can't yet reach — Wales + the Lakes for ITALIC water (Afon/Nant/tarns),
# Roman/prehistoric country (Hadrian's Wall, Wessex, Bath, Cornwall) for the BLACKLETTER antiquity fonts —
# plus a broad geographic spread. Feeds a decisive re-run of premise_test.py.
cd /vast/ishi/gb1900/probe/font
PY=/vast/ishi/envs/boundary/bin/python
# lon lat  region-note
SHEETS="
-5.051 50.263 truro-cornwall
-3.533 50.723 exeter-devon
-2.437 50.714 dorchester-dorset
-1.780 51.180 amesbury-stonehenge
-2.360 51.381 bath-roman
-4.083 52.415 aberystwyth-wales
-3.885 52.740 dolgellau-merioneth
-4.277 53.141 caernarfon-wales
-3.393 51.947 brecon-wales
-4.306 51.856 carmarthen-wales
-2.716 52.056 hereford
-1.257 51.752 oxford
0.119 52.205 cambridge
1.298 52.630 norwich
-0.541 53.234 lincoln
-1.080 53.959 york-riding
-1.700 53.900 wharfedale-yorks
-2.799 54.047 lancaster
-2.745 54.328 kendal-lakes
-1.573 54.777 durham
-2.100 54.985 hexham-hadrianswall
-2.716 52.368 ludlow
-1.911 53.259 buxton-peak
-2.244 51.864 gloucester
"
echo "$SHEETS" | while read lon lat note; do
  [ -z "$lon" ] && continue
  echo "=== $note ($lon,$lat) $(date +%H:%M:%S) ==="
  $PY discover_sheet.py --lon "$lon" --lat "$lat" --r 10 --workers 48 2>&1 | grep -vE "Warning|warn" | grep -E "sheet |labels=|matched|===|discover" || true
done
echo "BATCHDONE"
