# Handoff — build the gateway `POST/DELETE /api/links` receiver (contributor hard-link live forwarding)

**Status:** ✅ **IMPLEMENTED 2026-07-11** (branch `feat/api-links-receiver`). Requested by Stephen 2026-07-11.

> **What landed:** `gateway/links.py` (`POST` / `DELETE /api/links`, mounted in
> `gateway/app.py` before the catch-all proxy) + `tests/test_links_receiver.py`
> (8 tests, all green; existing gateway suite unaffected). Design option **A**
> (separate live-delta DB) — reuses `clustering.sqlite_overlay._INSERT_SQL`/
> `_row_tuple`/`initialise_schema` and `staging_contract.validate_hard_link_row`
> so the contract can't drift.
>
> **Resolved decisions (the three open questions below):**
> 1. **Consumption:** durability + next-rebuild inclusion, **not** real-time.
>    Nothing at reconcile time reads `hard_link_assertions` today (batch clustering
>    is the only consumer), so a live write doesn't change reconcile output until
>    re-clustering. A live reconcile-time union(batch, live-delta) lookup is a
>    separate feature. Documented in the module docstring.
> 2. **Path:** `HARD_LINK_LIVE_DB` env var, default
>    `{IX1_BASE}/hardlinks/hard_links_live.sqlite` (alongside the batch overlay).
>    ⚠ **Confirm/pin this with the team before prod deploy.**
> 3. **Auth:** none — matches `reingest.py` (IP allow-list); Django's optional
>    bearer is ignored, as in reingest.
>
> **Still open (separate tickets — flagged, not built here):**
> - **Batch side:** extend `contributor_replay.py` (or sibling) to harvest active
>   `api_contributorattestation` rows into the batch overlay **and prune the
>   live-delta** for rows now folded in — else the live-delta grows unbounded.
> - **Live reconcile consumption:** union(batch overlay, live-delta) at query time,
>   if/when real-time effect is wanted.
> - **Deploy:** land on Pitt, restart gateway (`gw restart`, `gazetteer` user),
>   smoke-test per the deploy notes; Django's `crc_post_link`/`crc_delete_link`
>   404s should then stop.
**Origin:** the WHG `website` repo already **forwards** every contributor `ContributorAttestation`
create/revoke to the CRC gateway at `POST` / `DELETE {CRC_GATEWAY_URL}/api/links` (see
`whg3/api/crc_client.py::crc_post_link` / `crc_delete_link`, wired via `whg3/api/signals.py`,
registered in `ApiConfig.ready()`). **The gateway has no such route**, so these calls currently 404
(logged best-effort on the Django side, no user impact). This handoff is to build the receiver.

Cross-refs: website `whg3/api/{crc_client,signals,models}.py`; this repo's
`clustering/harvest/contributor_replay.py` (Batch 12), `clustering/sqlite_overlay.py`,
`processing/staging_contract.py`. Decisions behind the taxonomy: place#25 (link taxonomy), place#32
(single-attestation feature), place#93 (legacy-link governance).

---

## The gap (verified against commit `8c74228`)

1. **Django forwards, gateway doesn't receive.** `gateway/app.py` mounts exactly five routers —
   `reconcile`, `search`, `places`, `extend`, `reingest`. There is **no** links module or `/api/links`
   route anywhere in `gateway/`, and `git log --all` shows one was never committed.
2. **The overlay is a read-only, atomically-swapped batch artifact.** `clustering/sqlite_overlay.py::
   ship_to_pitt` rsyncs a freshly built DB to `.<name>.incoming` then does a single atomic `mv` over
   the live file. `processing/staging_contract.py` states plainly: *"the gateway opens it read-only."*
   → **A live writer cannot write into that same file**: its rows would be clobbered on the next batch
   swap, and writing to a file that gets atomically replaced underneath open handles is unsafe.
3. **Nothing in the gateway currently SELECTs `hard_link_assertions`.** Grep across `gateway/` finds no
   reader. The table today feeds the **batch clustering** phase (`clustering/…`), not live reconcile
   queries. ⇒ **Open question (below): a live `/api/links` write may not change reconcile results until
   the next re-clustering run.** Confirm the intended consumption path before assuming end-to-end effect.
4. **Batch ingestion of *fresh* attestations isn't built yet.** `contributor_replay.py` reads **only**
   the legacy `place_link` / `close_matches` tables (its docstring: *"the legacy v3 schema has no
   contributor_attestations table … once the new flow ships"*). `retention_sweep.py` reads
   `api_contributorattestation` **only** for the 11/12-month retention timers, not to produce hard
   links. So today, live-created `ContributorAttestation` rows reach the index by **no path at all**.

---

## The exact wire contract (from `whg3/api/crc_client.py`, verified)

Django sends JSON with `_headers()` (includes `Authorization: Bearer <CRC_GATEWAY_API_KEY>` **iff**
that setting is non-empty) and a short timeout (`CRC_GATEWAY_TIMEOUT`, default 10s). Any 2xx = success;
anything else is a best-effort failure Django just logs.

**`POST /api/links`** — create/publish one assertion. Body:
```json
{
  "place_a":         "<ns>:<id>",     // canonical-ordered, place_a < place_b (Django enforces)
  "place_b":         "<ns>:<id>",
  "relation_type":   "sameAs" | "exactMatch" | "closeMatch" | "distinct",
  "source_category": "contributor",
  "source_id":       "contributor:<user_id>",   // NO :legacy_v3_2 suffix for live rows
  "asserted_at":     "<ISO 8601>" | null,
  "justification":   "..." | null
}
```
Only `status='active'` attestations are forwarded (Django filters; pending/rejected never leave the
platform). A row transitioned *out* of `active` is sent as a **DELETE** by Django.

**`DELETE /api/links`** — revoke one assertion, identified by the overlay's `UNIQUE` key. Body:
```json
{ "place_a": "<ns>:<id>", "place_b": "<ns>:<id>", "relation_type": "...", "source_id": "contributor:<user_id>" }
```
(Django sends the identifying fields as a JSON body on DELETE deliberately, to avoid URL-encoding four
fields.)

---

## The target schema (from `processing/staging_contract.py::HARD_LINK_SQLITE_SCHEMA`)

```sql
CREATE TABLE hard_link_assertions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    place_a TEXT NOT NULL,
    place_b TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    source_category TEXT NOT NULL,
    source_id TEXT NOT NULL,
    asserted_at TEXT,
    justification TEXT,
    CHECK (place_a < place_b),
    CHECK (relation_type IN ('sameAs', 'exactMatch', 'closeMatch', 'distinct')),
    CHECK (source_category IN ('authority', 'contributor')),
    UNIQUE (place_a, place_b, relation_type, source_id)
);
```
Inserts use `INSERT OR IGNORE` (idempotent). Reuse `processing.staging_contract.validate_hard_link_row`
and the column order in `clustering.sqlite_overlay._INSERT_SQL` / `_row_tuple` — **do not** re-derive the
contract. `source_category` for these rows is always `"contributor"`.

---

## THE key design decision — where do live writes go?

The batch overlay is read-only + atomically swapped, so live writes need their own home. Pick one
(recommended: **A**), and document the choice:

- **A. Separate live-delta DB (recommended).** The receiver writes to a *second* SQLite file on the
  Pitt VM (e.g. `hard_link_assertions_live.db`) with the **same schema**, that the batch swap never
  touches. Whatever consumes hard links (clustering and/or a future live reconcile lookup) reads the
  **union** of the batch overlay + the live-delta. On each batch run, once the "new flow" folds active
  `api_contributorattestation` rows into the freshly built batch overlay, the live-delta can be
  **pruned/reset** (everything in it is now in the batch DB). Clean separation; no clobbering; the
  receiver owns a file it can safely open read-write (WAL).
- **B. ATTACH + view.** Keep the batch overlay read-only; `ATTACH` the live-delta; expose a `UNION`
  view. Same data model as A, more SQL plumbing.
- **C. Write into the batch DB directly.** ❌ Rejected — clobbered on the next `ship_to_pitt` swap, and
  read-write against an atomically-replaced file is unsafe.

Whichever you choose, the **DELETE** path removes the matching row from the *live-delta* by the UNIQUE
key `(place_a, place_b, relation_type, source_id)`. (A revoke of a row that only exists in the batch
overlay can't be honoured live — note this; it's reconciled on the next batch run, which is exactly the
best-effort contract Django already assumes.)

---

## Implementation steps

1. **New module `gateway/links.py`** — `APIRouter(prefix="/api", tags=["Links"])`, modelled on
   `gateway/reingest.py` (structure, logging, pydantic models). Two routes:
   - `@router.post("/links", status_code=201)` → validate body (pydantic + `validate_hard_link_row`),
     enforce `place_a < place_b` (reject 422 if violated — Django already canonical-orders, so this is
     a guard), `INSERT OR IGNORE` into the live-delta DB. Return the row / `{"inserted": bool}`.
   - `@router.delete("/links", status_code=200)` → delete by the UNIQUE key from the live-delta.
     Return `{"deleted": <rowcount>}`. Idempotent (deleting a nonexistent row → `deleted: 0`, still 2xx).
2. **Mount it** in `gateway/app.py`: `from .links import router as links_router` +
   `app.include_router(links_router)` (alongside the existing five).
3. **DB access.** Add a small helper (or extend `sqlite_overlay`) to open the **live-delta** DB
   read-write with WAL, creating it + schema on first use via `initialise_schema`. Path from an env var
   (e.g. `HARD_LINK_LIVE_DB`, default under the same Pitt dir as the batch overlay — **confirm the live
   overlay path with the team**; batch ships to a dir like `/ix1/ishi/hardlinks` per
   `submit_hardlinks_slurm.py`, but the *gateway's* open path must be pinned).
4. **Auth.** Match the gateway's existing inbound convention. `reingest.py` uses **no** bearer (Pitt
   firewall whitelists the DO app-server IP; bearer is "reserved for the inverse-direction inventory
   push at `/api/registry/inventory`"). Django *does* send `_headers()` (a bearer when
   `CRC_GATEWAY_API_KEY` is set), so either (a) ignore it and rely on IP allow-listing like reingest, or
   (b) verify it if inventory-style bearer auth is standard here. **Decide and match the sibling routes.**
5. **Concurrency.** Live writes are low-volume (one per contributor action) but can overlap a batch
   swap. With option A the batch swap only touches the *batch* file, so the live-delta is unaffected;
   still open the live DB with WAL and a short busy-timeout.
6. **Tests** (`tests/test_links_receiver.py`) — mirror `tests/test_sqlite_overlay.py`: POST inserts a
   valid row; duplicate POST is a no-op (INSERT OR IGNORE); DELETE removes by key and is idempotent;
   `place_a >= place_b` → 422; bad `relation_type` / `source_category` → 422; round-trip via FastAPI
   `TestClient`.

---

## Related / prerequisite work (flag to the team — may be separate tickets)

- **Consumption.** Confirm what reads `hard_link_assertions` at reconcile time. If clustering is the
  only consumer (batch), a live `/api/links` write won't change reconcile output until the next
  re-clustering run — in which case the receiver's value is durability + faster eventual inclusion, and
  a **live reconcile-time hard-link lookup** (union of batch + live-delta) is a separate feature to
  build for true real-time effect. Decide the intended semantics before promising "real-time."
- **The "new flow" batch side.** Extend `contributor_replay.py` (or a sibling) to also harvest active
  `api_contributorattestation` rows (`source_id = contributor:<user_id>`, no `:legacy_v3_2` suffix) into
  the batch overlay, and to **prune the live-delta** for rows now present in the batch build. Without
  this, the live-delta grows unbounded and never gets folded into clustering.

---

## Deployment notes

- The gateway runs on the **Pitt VM**. Per the deploy convention, code lands via push+pull and only the
  `gazetteer` user can restart it (`gw restart`) — a non-`gazetteer` operator must coordinate the
  restart. Ship the new route + confirm `/api/health` still OK, then smoke-test:
  `curl -X POST $CRC_GATEWAY_URL/api/links -H 'content-type: application/json' -d '{…valid row…}'` → 201;
  repeat → 201 with `inserted:false`; `DELETE` → `deleted:1`.

## Acceptance criteria

- `POST /api/links` with a valid contributor row inserts (idempotently) into the live-delta and returns
  2xx; the row is visible to whatever unions batch+live-delta.
- `DELETE /api/links` removes the row by UNIQUE key, idempotently, 2xx.
- Invalid rows (`place_a >= place_b`, bad enum) → 422; the batch overlay file is never opened
  read-write.
- Django's `crc_post_link` / `crc_delete_link` (already live on prod) stop logging 404s.
- The live-delta ↔ batch-overlay lifecycle (pruning on batch runs) is documented, even if the batch
  side is a follow-up ticket.

## Open questions for Stephen / team

1. Does reconcile need a **live** hard-link lookup, or is batch re-clustering the only consumer? (Sets
   whether this receiver is "real-time" or "durability + next-rebuild".)
2. Exact **live-delta DB path** the gateway should own on the Pitt VM.
3. Inbound **auth**: IP allow-list only (like reingest) or verify Django's bearer?
