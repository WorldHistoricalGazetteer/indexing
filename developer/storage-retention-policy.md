# Storage retention policy: Pitt project storage (`ishi`)

**Status:** adopted 29 September 2026 (Stephen Gadd, WHG Technical Director).
**Scope:** everything the `ishi` group keeps on `/ix1/ishi` (bulk, HDD) and `/vast/ishi` (flash).
**Why:** `/ix1` reached 92% of its 5 TB quota on 28 September 2026 and triggered a CRC warning.
The clean-up that followed brought it to ~60%. This policy exists so the same things do not
accumulate again. The measurements and the reasoning are in place Discussion #304.

Check usage with `crc-quota`. **Review this policy whenever either tier passes 80%.**

## What we keep, and how many

| Class | Keep | Delete / notes |
|---|---|---|
| **ES snapshots: `staging_repo`** | the **latest 5** snapshot generations | Delete **only through the ES API**, never by removing files. `staging_repo` is registered **read-only** on the production cluster: to prune, re-register it with `readonly: false`, delete, then set it back to `readonly: true` in the same script (use a `trap` so read-only is restored even on failure). Check `_snapshot/_status` is empty first. |
| **ES snapshots: `prod_repo`, `cluster_exchange`** | as they are (6 and 3 on 29 Sep 2026) | Not yet under a count rule. Ask before pruning. |
| **Toponym working databases** (`/ix1/ishi/data/toponyms*.db`) | the **current** generation, **one previous**, and the base `toponyms.db` | Pre-migration backups (`*.pre-*`) and superseded generations go once the replacement is verified. |
| **Raw authority dumps** (OSM, Wikidata, `data/authorities/`) | **keep** | Needed for re-ingest. Candidates for the archive tier, not for deletion. |
| **Elevation (Terrarium)** | the full-resolution tileset `tiles/terrarium.mbtiles` | It is the asset for self-hosting terrain (dropping the dependency on AWS-hosted elevation tiles). The loose PNG source is **not** retained; re-fetch it from the public Terrarium dataset if a rebuild is ever needed. |
| **GB1900** | **keep all**, including `gb1900_tiles.tar` | The tar is the only copy of zoom 16 (the `gb1900_tiles17` bands are zoom 17 only). |
| **LLM / VLM model cache** (`/ix1/ishi/hf_cache`) | models in active use | Remove superseded checkpoints **by deleting the model folder** (`hub/models--*`). **Never delete from `hub/blobs/` directly**: the cache uses a two-hop layout (model `blobs/` symlinks into the shared `hub/blobs/xx/…` store), so resolve with `readlink -f` and confirm no other model references a blob. |
| **Retired working copies** (e.g. `elastic.retired-*`) | until the replacement is verified | Then delete. |

## How to delete safely

- **Never run heavy scans or mass deletions on the login node (`crc0`).** Use a Slurm job
  (`sbatch -M htc`; the `htc-htc-s` QoS caps jobs at **2 hours**, so split large trees into an
  array) or run on the gazetteer VM under `nice -n 19 ionice -c3`.
- **Verify before deleting a supposed duplicate.** Compare contents (paths and sizes, per zoom
  level for tiles), not names: the GB1900 "duplicate" tar turned out to hold a different zoom level.
- **Measure `crc-quota` before and after**, and record what was removed and why.

## Permissions, so that any `ishi` member can tidy up

- Every account that writes to `ishi` storage uses **`umask 002`**: in `~/.bashrc`, at the start
  of cron lines (`umask 002; …`), in Slurm scripts, and as `UMask=0002` in any systemd unit.
  Otherwise new directories come out `755`, and another group member cannot delete their
  contents (deletion needs write permission on the **directory**).
- **Exceptions (owner-only write, deliberately):** the live Elasticsearch data directory
  (`/vast/ishi/es/data`) and the certbot tree (`/ix1/ishi/certbot`).
- One-off fix for existing directories (run as the owning account; it touches only that account's
  directories): `find <tree> -user <account> -type d ! -perm -g+w -exec chmod g+w {} +`.
