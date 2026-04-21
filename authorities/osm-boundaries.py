#!/usr/bin/env python3
# authorities/osm-boundaries.py

"""Deprecated legacy entry point for the removed boundaries-index workflow."""

import sys


def main():
    print("DEPRECATED: authorities.osm-boundaries no longer writes to a separate boundaries index.")
    print("OSM/OHM full geometries are completed in the places index via osm-places.py / ohm-places.py.")
    print("Use `es -ingest -n osm,ohm` for ingestion and `es -generate-tiles` for tilesets.")
    raise SystemExit(1)


if __name__ == '__main__':
    main()
