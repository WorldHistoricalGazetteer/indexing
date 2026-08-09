# Handover — geom store rebuilt; tiling deferred (place#176)

**Written 2026-08-09, before SG's week away. Read this first.**

Priority on return, in order: (1) decide `un`, (2) delete the broken-store copy from
`/ix1`, (3) the deferred retile, (4) the whg3 map filter that started all this.

**The pending merge that was originally item 1 is DONE** (2026-08-09, before SG left) —
see §1.

---

## Current state — what is live and working

**The geom store is FIXED and serving.** `/vast/ishi/geom` holds **11,754,936**
geometries across 82 shards. Verified against the live index: a 2,002-geometry
random sample resolved 2,001, with **0 bounds mismatches** — that second number is
the one that matters, because a mis-aligned rebuild would still "resolve" every key.

So the gateway's `containment=exact` path and `/api/places` geometry serving are
correct again. **Nothing user-facing is degraded.**

Per-namespace, against the index's `geom_ref` counts:

| namespace | expected | in store |
|---|---:|---:|
| osm | 10,871,752 | 10,871,752 ✅ exact |
| ohm | 755,653 | 755,653 ✅ exact |
| wd | 57,440 | 58,610 ✅ |
| kain_par, po, nl, hgis, pl, vob_lgd, vob_rd | — | ✅ reconciled |
| **clio** | 15,690 | **12,704** ⚠️ see below |
| **un** | 247 | **0** ⚠️ needs your decision |
| **ukhc / vob_cty / vob_rc** | 92 / 65 / 55 | **0** ⚠️ extracted, not yet merged |

Backups now exist: consolidation copies `index.sqlite` to
`$IX1_BASE/backups/geom/index-<stamp>.sqlite` (keeps 3). First one:
`/ix1/ishi/backups/geom/index-20260809T064045Z.sqlite`. **This did not exist before
and is why the original loss was unrecoverable.**

### 🗑️ Delete the broken store copy — SG asks for this as soon as practical

The old broken store was moved off `/vast` on 2026-08-09 to
**`/ix1/ishi/DELETABLE-AFTER-2026-08-31--geom-broken`** (57 GB, 249 shards, plus a
`README-DELETE-ME.txt` explaining itself).

**It is non-essential and should be deleted as soon as is practical** — SG's
instruction. It is retained only as short-term insurance while the rebuilt store
beds in; it cannot be read (its index was truncated to 2 rows, and the shards are
keyless WKB), so it has no standalone value. The end-of-August date in the
directory name is a backstop, not a target.

```bash
rm -rf /ix1/ishi/DELETABLE-AFTER-2026-08-31--geom-broken
```

Reasonable trigger: once the pending merges in §1 are done and the store has served
normally for a few days. If you want a single gate — delete it once §1 and §2 are
closed.

---

## 1. Merge the pending geometry — ✅ DONE 2026-08-09

Completed before SG left. `clio` (re-extracted with the `b3e0dd0` fix), `ukhc`,
`vob_cty` and `vob_rc` were merged into the live store:

```
consolidate_geom_store: merging — kept 11,754,936 existing entries, new shards start at 0083
consolidate_geom_store: wrote 16,536 geometries across 84 shards → /vast/ishi/geom
write_sqlite_index: wrote 11,758,768 rows → /vast/ishi/geom/index.sqlite (391 MB)
consolidate_geom_store: backed up index.sqlite → /ix1/ishi/backups/geom/index-20260809T084521Z.sqlite
```

**11,758,768** = 11,754,936 + clio's 2,986 newly-addressable + 846 from the three
small namespaces. clio verified at 15,690 entries / 15,690 distinct keys / 0
duplicates, exactly the index's count.

Only the merged namespaces were re-packed — staging was split so the 22 GB already
in the store wasn't needlessly rewritten. The merged inputs are parked at
`/vast/ishi/geom_rebuild/staging_pending`; the rest remains in
`/vast/ishi/geom_rebuild/staging`. Both are now redundant and can be removed once
you're happy (they are the only copy of nothing — the store has it all).

⚠️ **If you ever re-extract a namespace, delete its `*.bin` + `*.index.json` from
staging first** — `GeomStoreWriter` opens with `"ab"` and *appends*, so a re-run
over populated staging silently doubles every entry.

## 2. `un` — YOUR DECISION, deliberately untouched

247 geometries, currently absent from the store. I stopped work on this because you
flagged it: *"we're relying on two different sources for different purposes"* and
*"we use a much more precise geoBoundaries dataset for that"*.

What I established, so you don't have to re-derive it:

- Live `un` geometries are stamped `boundary_source=geoboundaries`, refs `un:<iso3>_0`.
- `authorities/un-countries.py` writes geom namespace `un`, and its own header says
  BNDA (`processing/data/un_bnda_countries.geojson`) is the **metadata** source —
  iso2cd / iso3cd / m49_cd — so geometry comes from elsewhere.
- `un-geoscheme-boundaries.py` writes a *different* geom namespace, `osm_geoscheme`.
- The staged tree is named `un.bnda-baseline`, not `un`.
- `/ix1/ishi/data/authorities/un/countries.geojson` (14 MB, 5 May) exists, but I did
  not confirm it is the high-precision geoBoundaries set you meant.

I ran `submit_extract_slurm --namespace un` before your warning and **cancelled it**.
It had written `un.bin` with no `un.index.json`; consolidation discovers work through
the index files, so nothing leaked. I then deleted that orphan. **Staging is clean.**

`un` is not in the retile bucket set either, so nothing is blocked on it.

---

## 3. clio — fixed, but the fix CHANGES THE MAP

Root cause (fixed in `b3e0dd0`): duplicate polities were disambiguated with `_v{n}`
in the *caller*, after `process_cliopatria_feature` had already written the polygon
under the pre-rename key. So the store key and the doc's `geom_ref` diverged, leaving
**2,986 dangling refs**.

**These have been dangling in production all along** — a z10 sweep of the deployed
clio tileset found 1,235 distinct place_ids and not one `_vN`. Those polities have
never rendered. The rebuild reproduced the bug faithfully; it did not introduce it.

⚠️ So merging the fixed clio adds ~2,986 polygons that have **never been on the map**.
Correct, but a visible change to the Cliopatria layer — worth a look before it ships.

---

## 4. Deferred: the retile (and what it was all for)

**Deliberately deferred** — tilesets are consumed only by the BETA/Atlas UI, so
nothing production-facing waits on it.

The original goal (place#176) was the Atlas temporal filter. Everything except the
tiles is live: two query modes, the corrected envelope, refitted clustering weights.
The tiles are the last piece, and they need regenerating for two reasons:

1. Existing tiles carry collapsed `(2026, 2026)` stamps, so switching the map date
   filter on today blanks `osm` / `tgn` / `nl`.
2. No existing tile carries `start_def` / `end_def`, the props that let the map
   express *definitely* mode rather than only *possibly*.

Both are already fixed in the tile builder (`c5c209c`). To run it:

```bash
python -m processing.submit_tiles_slurm --run-id h3ccode-20260805T120000Z
# sbatch scripts + array maps are already written under
#   /vast/ishi/staged/runs/h3ccode-20260805T120000Z/
# 23 buckets: osm, ohm, osm_misc + 20 smaller. Tiers: 18×mem16g-t4h,
# gb/tgn mem32g-t12h, ohm mem96g-t24h, osm+osm_misc mem192g-t24h.
```

**For `gn` and `wd`, set `TILE_ES_DOC_NAMESPACES=gn,wd`** — their staged trees are
still test stubs (see §5), but `71bcc39` lets the tile builder read documents from
the places index instead. Verified: 3,000 real gn docs → 3,000 features, 0 skips.
`gn` needs no geom store at all (points-only, resolves from `repr_point`).

Afterwards, the last whg3 change is the two-mode `setFilter` on the map layers —
a few lines, spelled out in place#176.

---

## 5. Landmines — please don't re-arm these

**NEVER run `python -m unittest discover -s tests` against real settings.** It puts
`tests/` on `sys.path` and imports modules top-level, so `tests/__init__.py` never
runs and its sandbox is never installed — every writing test then targets the real
`/vast` paths. That is exactly how the geom store was destroyed on 2026-08-07 (a
two-feature synthetic store overwrote the live index and shard 0001), and how `gn`
and `wd` staging became 1.5–5 KB stubs.

Guard shipped in `645f3f2`: the writing tests now call `assert_sandboxed()` and
refuse to run against real storage. Safe form is package-qualified:

```bash
python -m unittest tests.test_update_merge        # safe
python -m unittest discover -s tests -t .          # safe (-t . keeps the package)
```

**`gn` and `wd` staged trees are still stubs.** Only matters for future re-ingestion
cycles (staging is the pipeline's canonical input) — tiling no longer needs them, per
§4. `wd`'s extract was re-run into `/vast/ishi/staged_geomrebuild/wd/` and could be
promoted rather than re-run. `nl` and `un` staged data was already missing before
any of this.

**Registry `temporal_extent` re-push is NOT needed** — 20 of 23 recomputed aggregates
matched `/api/sources/` exactly. Do not push blind: `chgis` and `hgis` staged
snapshots are now *thinner* than the registry, so a push would move them backwards.

---

## Reference — where things are

| | |
|---|---|
| live store | `/vast/ishi/geom` (82 shards, 11,754,936 rows) |
| old broken store | `/ix1/ishi/DELETABLE-AFTER-2026-08-31--geom-broken` (57 GB — **delete ASAP**) |
| pending staging | `/vast/ishi/geom_rebuild/staging` |
| index backups | `/ix1/ishi/backups/geom/` |
| rebuild run id | `geomrebuild-20260807T170000Z` |
| index run id (for tiles) | `h3ccode-20260805T120000Z` |
| source dumps | `/ix1/ishi/data/authorities` (**IX1**, not `/vast/ishi/data`) |
| verification script | `~/verify_store.py` on `pitt` |

Slurm: `crc0` is the **smp login node** — never run compute there. Submit to htc with
`sbatch -M htc`, query with `squeue -M htc` / `sacct -M htc`. Files a job reads must
be on `/vast`, not the login node's `/tmp`.
