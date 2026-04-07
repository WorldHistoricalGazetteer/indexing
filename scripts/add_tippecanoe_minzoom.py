#!/usr/bin/env python3
"""
Post-process a boundaries GeoJSON Lines file to add tippecanoe:minzoom
properties based on admin_level.

Reads from stdin or a file, writes to stdout.  Designed to be piped
directly into tippecanoe or used to produce a patched file:

    # Pipe directly into tippecanoe (no temp file needed):
    python scripts/add_tippecanoe_minzoom.py boundaries.geojsonl | \
        tippecanoe --layer boundaries -o boundaries.mbtiles -

    # Or produce a patched file:
    python scripts/add_tippecanoe_minzoom.py boundaries.geojsonl > boundaries_z.geojsonl
"""

import sys
import orjson

ADMIN_LEVEL_MINZOOM = {
    2: 0,
    3: 2,
    4: 3,
    5: 4,
    6: 5,
    7: 6,
    8: 7,
    9: 8,
    10: 9,
}


def main():
    src = open(sys.argv[1], 'rb') if len(sys.argv) > 1 else sys.stdin.buffer
    out = sys.stdout.buffer
    count = 0

    for line in src:
        line = line.strip()
        if not line:
            continue
        try:
            feature = orjson.loads(line)
            props = feature.get('properties', {})
            admin_level = props.get('admin_level')
            if admin_level is not None:
                minzoom = ADMIN_LEVEL_MINZOOM.get(int(admin_level), 0)
                if minzoom > 0:
                    props['tippecanoe:minzoom'] = minzoom
            out.write(orjson.dumps(feature))
            out.write(b'\n')
            count += 1
            if count % 100000 == 0:
                print(f"\r  Processed {count:,} features", end='', file=sys.stderr)
        except Exception:
            out.write(line)
            out.write(b'\n')

    if src is not sys.stdin.buffer:
        src.close()

    print(f"\n  Done: {count:,} features processed", file=sys.stderr)


if __name__ == '__main__':
    main()

