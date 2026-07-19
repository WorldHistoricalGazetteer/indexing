#!/bin/bash
# Scale the MapReader spotter to 16 more GB sheets (on top of the 4-sheet pilot) for a richer font reference.
# CPU on pitt, 5 in parallel (each thread-capped) to avoid thrash. ~21 min/sheet -> ~4 waves -> ~90 min.
cd /vast/ishi/gb1900/probe/font
PY=/home/stg135/.conda/envs/mapreader/bin/python
CENTRES="
-5.051 50.263 truro
-3.533 50.723 exeter
-2.360 51.381 bath
-3.885 52.740 dolgellau
-4.277 53.141 caernarfon
-3.393 51.947 brecon
-4.306 51.856 carmarthen
-2.716 52.056 hereford
-1.257 51.752 oxford
0.119 52.205 cambridge
-0.541 53.234 lincoln
-1.700 53.900 wharfedale
-2.799 54.047 lancaster
-2.745 54.328 kendal
-1.573 54.777 durham
-2.100 54.985 hexham
"
echo "$CENTRES" | grep -v '^$' | xargs -P 5 -n 3 bash -c '
  OMP_NUM_THREADS=5 MKL_NUM_THREADS=5 OPENBLAS_NUM_THREADS=5 \
  '"$PY"' spot_sheet.py --lon "$0" --lat "$1" --tag "$2" --r 8 > spot_"$2".log 2>&1
  echo "done $2"'
echo "SPOTBATCHDONE"
