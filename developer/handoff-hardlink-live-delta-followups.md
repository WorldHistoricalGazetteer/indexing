# Handoff — follow-ups for the `/api/links` live-delta receiver

**Status:** open. Created 2026-07-11 after the receiver landed
(`gateway/links.py`, branch `feat/api-links-receiver`;
see `developer/handoff-api-links-receiver.md`).

The receiver writes each live contributor hard-link (create/revoke forwarded by
whg3 `crc_post_link`/`crc_delete_link`) into a **live-delta** SQLite at
`{IX3_BASE}/hardlinks/hard_links_live.sqlite` (`/vast/ishi/hardlinks/…`) with the
same schema as the batch overlay. It is deliberately a *second* file — the batch
overlay is read-only + atomically swapped. Two follow-ups make it fully useful.
They are independent and can ship in either order.

---

## Ticket A — Batch-side harvest of fresh attestations + live-delta prune

**Problem.** Today the batch harvest (`clustering/harvest/contributor_replay.py`)
reads only the **legacy** `place_link` / `close_matches` tables. Fresh
`ContributorAttestation` rows (the new live flow) reach the batch overlay by **no
path**, and the live-delta is never pruned, so it **grows unbounded**.

**Do.**
1. Extend `contributor_replay.py` (or a sibling harvester) to also read **active**
   `api_contributorattestation` rows from DO PostgreSQL —
   `source_id = "contributor:<user_id>"`, **no** `:legacy_v3_2` suffix,
   `status = 'active'` — and fold them into the freshly built batch overlay
   alongside the legacy tables. (Contract: `processing.staging_contract`
   `validate_hard_link_row`; `source_category = "contributor"`.)
2. After a successful batch build + `ship_to_pitt`, **prune the live-delta**: its
   active rows are now a subset of the fresh batch overlay. Simplest safe approach
   — delete live-delta rows with `asserted_at <= <batch harvest start timestamp>`
   (leaves in-flight rows created during the build; avoids a lost-write race). A
   blunt `DELETE FROM hard_link_assertions` is acceptable only if the batch harvest
   provably captured every active row first.

**⚠ File permissions (verified live 2026-07-11).** The gateway (`gazetteer`)
creates the live-delta as `-rw-r--r--` (owner `gazetteer`, group `ishi`,
group-**readable** but NOT group-**writable**). So the prune, if run as a different
user (e.g. `stg135` for the Slurm batch), can READ the file but not DELETE from it.
Resolve one of: run the prune as `gazetteer`; have the gateway create the file
group-writable (open it with a group-write umask, or `chmod g+w` on create); or
otherwise coordinate ownership. Decide this before wiring the prune.

**Cross-refs.** `clustering/harvest/contributor_replay.py`, `gateway/links.py`
(`LIVE_DB_PATH`), whg3 `api/models.py::ContributorAttestation`, place#93
(legacy-link governance — the "no bulk migration" principle still holds).

**Acceptance.** Fresh active attestations appear in the batch overlay on the next
build; the live-delta stays bounded (pruned each build); no double-counting a row
present in both stores.

---

## Ticket B — Live reconcile-time consumption (union batch overlay + live-delta)

**Problem.** Nothing at **reconcile time** reads `hard_link_assertions` yet — the
batch clustering phase is the only consumer. So a live `/api/links` write gives
durability + faster next-rebuild inclusion, **not** real-time reconcile effect.

**Do.** When the gateway performs hard-link expansion for a result set, read the
**union of the batch overlay + the live-delta** (both opened read-only; `ATTACH`
one to the other, or two queries merged in Python), deduplicated by the
`(place_a, place_b, relation_type, source_id)` UNIQUE key. Pending contributor
assertions (DO Postgres, scope-filtered) are merged separately at the Django side
per Master Plan Part VII — this ticket is only the two Pitt-side SQLite files.

**Scope note.** This is part of the larger **"hard-link expansion + ship"** gateway
item in the clustering re-architecture (`developer/plan-outstanding-2026-07.md`
§1) — implement it *with* that work, not as a standalone bolt-on. It's what makes
a `POST /api/links` visible in reconcile without waiting for a rebuild.

**Cross-refs.** `plan-outstanding-2026-07.md` §1 (Gateway → "Hard-link expansion +
ship"), `plan-dynamicClustering.DEPRECATED.md` §2 (query-time expansion),
`gateway/links.py`, `clustering/sqlite_overlay.py`.

**Acceptance.** A hard link created via `POST /api/links` is reflected in the
gateway's reconcile hard-link expansion without a re-clustering run; a `DELETE`
removes it likewise.
