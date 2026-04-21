# Ingestion Guide

This is a practical runbook for re-running authority ingestion on staging.

It reflects current behavior in:
- `scripts/es.sh`
- `scripts/ingest.sh`
- `processing/ingest_all_authorities.py`
- `processing/geom_store.py`

## 1) Preconditions

- Work on CRC host where WHG ingestion runs (typically via `ssh crc0` then `ssh pitt` as needed for your environment).
- Repo path assumed in examples: `/ix1/ishi/elastic`
- Conda env: `whg`
- `es` alias installed (`es -install`) or run with `source scripts/es.sh ...`

## 2) Start/Check Staging ES

Use staging before ingestion.

```bash
cd /ix1/ishi/elastic
source scripts/es.sh -staging-start --no-snapshot
es -staging-status
es -staging-health
```

Optional log tail:

```bash
es -staging-logs
```

## 3) Run Ingestion

`es -ingest` submits a Slurm job that runs:

```text
python -m processing.ingest_all_authorities ...
```

### Full ingestion

```bash
es -ingest -r
```

- `-r` / `--replace-existing` removes existing namespace docs before re-indexing.

### Namespace-limited ingestion

```bash
es -ingest -n wd -r
es -ingest -n osm,ohm -r
es -ingest -n gn,wd,osm,ohm -r
```

Important behavior:
- Namespace selection includes all scripts for that namespace in configured order.
- For `wd`, this includes both:
  - `authorities/wikidata-places.py`
  - `authorities/wikidata-geoshapes.py`

## 4) Geometry Storage Behavior (Phase A)

During ingestion:
- `geometries[].geom` is no longer written to ES.
- Each geometry entry carries `has_geom`.
- Top-level `h3_centroid` and `h3_cover` are computed.
- Full geometries are staged as WKB in VAST staging dir (default from `processing/settings.py`):
  - `GEOM_STORE_STAGING_DIR=/vast/ishi/geom/staging`

After ingestion finishes, run consolidation to produce final shard files + `index.json`:

```bash
conda run -n whg python -m processing.geom_store
```

Default final store dir:
- `GEOM_STORE_DIR=/vast/ishi/geom`

## 5) Verify Ingestion Quickly

### Check Slurm job output

```bash
es -staging-logs
```

### Basic index counts

```bash
conda run -n whg python -m processing.ingest_all_authorities --skip-counts --check-only
```

### Spot-check fields on ES docs

```bash
ES_PASS=$(cat /ix1/ishi/es/config/elastic.password)
curl -s -u "elastic:${ES_PASS}" "http://localhost:9200/places*/_search" \
  -H "Content-Type: application/json" \
  -d '{"size":1,"query":{"prefix":{"place_id":"wd:"}},"_source":["place_id","h3_centroid","h3_cover","geometries.has_geom","geometries.repr_point","geometries.hull","geometries.bounds"]}'
```

Expected:
- `h3_centroid` exists
- `h3_cover` exists
- `geometries[].has_geom` exists
- no `geometries[].geom`

### Verify consolidated VAST files exist

```bash
ls -lh /vast/ishi/geom/index.json
ls -lh /vast/ishi/geom/geom_shard_*.bin | head
```

## 6) Common Re-run Patterns

### Re-run only Wikidata (places + geoshapes)

```bash
es -ingest -n wd -r
conda run -n whg python -m processing.geom_store
```

### Re-run OSM/OHM and then consolidate geometries

```bash
es -ingest -n osm,ohm -r
conda run -n whg python -m processing.geom_store
```

### Re-run everything from scratch on staging

```bash
es -ingest -r
conda run -n whg python -m processing.geom_store
```

## 7) Stop Staging When Done

```bash
source es.sh -staging-stop
```

## Notes

- `es -ingest` does not itself run geometry-store consolidation; run `python -m processing.geom_store` after ingestion.
- Gateway `geom: "full"` VAST read-path is a separate integration step; ingestion/consolidation can still be validated independently.

