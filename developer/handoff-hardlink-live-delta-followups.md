# Handoff — follow-ups for the `/api/links` live-delta receiver

**Status:** ✅ **CLOSED 2026-07-11.** Ticket A shipped to `main` (commits
`c17314b`…`94ae401`): batch harvest of active `api_contributorattestation` rows
+ post-ship live-delta prune + group-writable live-delta + `PG_DB_NAME` default
fix. Live dry-run verified against `whgv3beta` (legacy path live; attestation
query correct, table still empty until contributor links flow). Gateway deployed
+ restarted. The storage side is now self-maintaining. Ticket B is **not** work
for this doc — it lands with `developer/plan-outstanding-2026-07.md` §1 (the
clustering re-architecture, gated on the whg3 main↔atlas consolidation). Nothing
further to do here.

Created 2026-07-11 after the receiver landed (`gateway/links.py`, branch
`feat/api-links-receiver`; see `developer/handoff-api-links-receiver.md`).

The receiver writes each live contributor hard-link (create/revoke forwarded by
whg3 `crc_post_link`/`crc_delete_link`) into a **live-delta** SQLite at
`{IX3_BASE}/hardlinks/hard_links_live.sqlite` (`/vast/ishi/hardlinks/…`) with the
same schema as the batch overlay. It is deliberately a *second* file — the batch
overlay is read-only + atomically swapped. **Ticket A below is the one actionable
follow-up here** (indexing-side, no clustering dependency). Ticket B turned out
*not* to be standalone and has been **folded into plan §1** — see below.

---

## Ticket A — Batch-side harvest of fresh attestations + live-delta prune

**Status:** ✅ **IMPLEMENTED 2026-07-11** (branch `feat/ticketA-attestation-harvest`).
Code + tests landed; **operational steps remain** (deploy gateway, prod dry-run,
wire prod submit args) — see "Remaining / deploy" at the end of this section.

> **What landed**
> 1. **Harvest (part 1)** — `contributor_replay.py` now also reads **active**
>    `api_contributorattestation` rows (`_ACTIVE_ATTESTATION_QUERY`, no `ds_status`
>    filter — the row's own `status='active'` is the publish gate, matching exactly
>    what Django forwards). `_attestation_to_hard_link` mirrors
>    `ContributorAttestation.source_id()` byte-for-byte (`contributor:<uid>`, plus
>    `:legacy_v3_2` only for the rare inherited-and-active row) so a row in both the
>    batch overlay and the live-delta dedups on the identical UNIQUE key. Confirmed
>    the model schema against `whg3 api/models.py` + `api/signals.py`.
> 2. **Prune (part 2)** — `clustering.sqlite_overlay.prune_live_delta` SSHes to Pitt
>    and `DELETE`s live-delta rows with `asserted_at <= <cutoff>` (leaving NULL /
>    in-flight rows). The cutoff is captured in the sbatch **before** any harvest
>    (`HARDLINK_HARVEST_START`, ISO-8601 UTC, Django's format). `submit_hardlinks_slurm.py`
>    runs it after a successful `ship_to_pitt`, **best-effort** (a prune failure never
>    blocks the completion marker). New flags: `--pitt-live-db`, `--skip-prune`.
> 3. **File permissions** — resolved via the lowest-coordination option: the gateway
>    now creates the live-delta (+ its `-wal`/`-shm`) **group-writable** (umask `0o002`
>    + a best-effort `g+w` re-assert on every connect in `gateway/links.py::_connect`),
>    so an `ishi`-group batch user (`stg135`) can prune it. No ownership coordination
>    needed. (Alternative, if preferred: run the prune as `gazetteer` — pass
>    `--pitt-user gazetteer`.)
> 4. **Tests** — `test_contributor_replay.py` (attestation mapping, source_id parity,
>    relation-type/self-loop/user guards, 3-source counts, back-compat) +
>    `test_sqlite_overlay.py` (runs the exact `_PRUNE_REMOTE_PY` snippet locally:
>    cutoff boundary, NULL-keep, in-flight-keep). 37 passed.
>
> **Prod dry-run — ✅ done 2026-07-11** (CRC compute node → DO PG `whgv3beta`).
> Legacy path confirmed live: 34,569 `place_link` + 3,206 `close_match` harvested,
> correct `contributor:<uid>:legacy_v3_2` source_ids, canonical ordering. New
> attestation query executes against the real `api_contributorattestation` schema
> and returns **0 active rows** — expected: the live `/api/links` receiver only
> landed 2026-07-11, so the table is still empty. `attestation_input` /
> `attestation_converted` stay 0 until contributor links flow through the receiver;
> the mapping is unit-tested for then. (NB `clustering/config.py` `PG_DB_NAME`
> default `whgv2` is stale — the live DB is `whgv3beta`; prod works via `.env.local`
> override, but the default is misleading. Separate cleanup.)
>
> **Remaining / deploy**
> - Deploy `gateway/links.py` to Pitt + `gw restart` (as `gazetteer`) so the live-delta
>   is (re)created group-writable — an already-existing `-rw-r--r--` file is upgraded on
>   the next gateway write by `_ensure_group_writable`.
> - The prod `submit_hardlinks_slurm` invocation must pass `--pitt-user/--pitt-host/--pitt-dir`
>   (already required for shipping) so the prune runs; `--pitt-live-db` defaults to
>   `{IX3_BASE}/hardlinks/hard_links_live.sqlite` (matches `gateway/links.py::LIVE_DB_PATH`).

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

## Ticket B — REDUNDANT (folded into plan §1) — do NOT build separately

"Live reconcile-time consumption" (union of batch overlay + live-delta so a
`POST /api/links` affects reconcile immediately) is **not a standalone task**: the
gateway does **no** reconcile-time hard-link expansion today, so this is a
*requirement on building that expansion*, which is the **"Hard-link expansion +
ship"** item in `developer/plan-outstanding-2026-07.md` §1. That item now
explicitly says to read the **union of the batch overlay + the live-delta** (dedup
by the `(place_a, place_b, relation_type, source_id)` UNIQUE key), with pending
assertions merged at Django from DO Postgres (Master Plan Part VII).

So there is nothing to do under "Ticket B" here — it lands when §1's client-side
clustering hard-link expansion is built, which is **gated on the whg3 main↔atlas
consolidation** (the browser side that consumes the shipped edges).

---

## After Ticket A — where the remaining hard-link work lives

Once Ticket A ships, the receiver is fully self-maintaining: the live-delta stays
bounded and fresh contributor links are folded into the batch overlay each build.
The **storage side is then complete**. Everything left is *consumption*, and it
lives in `developer/plan-outstanding-2026-07.md` §1 (the clustering
re-architecture), **not here**:

- gateway **hard-link expansion + ship** — reads union(batch overlay + live-delta),
  emits edges to the browser (this subsumes old Ticket B);
- `clustering.js` — browser-side scoring + Union-Find;
- delete the stale `clusters` index.

That work is gated on the whg3 **main↔atlas** consolidation. So the next action
after Ticket A is **not to open a new hard-link ticket here** — it's to pick up
§1 once that gate clears. This follow-up doc can be closed when Ticket A is done.
