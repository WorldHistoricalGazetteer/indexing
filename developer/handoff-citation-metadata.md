# Handoff — Authority Citation / Licence / Rights Metadata (Batch 11 upgrade)

**Audience:** Claude Code running in the `indexing` repo (`/home/stephen/PycharmProjects/indexing`).
**Status:** ready to execute — the WHG (Django) side is built, deployed, and live (atlas/dev; prod parity follows at the atlas→main promotion).
**Origin spec:** §10 of `whg3/developer/plan-citations-licences-credit.prompt.md` (the WHG repo). This doc is the executable counterpart of that spec.

---

## 0. Why

Authority gazetteers today carry a **single free-text `citation` blob** per `AUTHORITIES` entry, which the Batch 11 push (`processing/push_gazetteer_inventory.py`) sends as the registry row's `description`. The blob mixes citation + licence + URL, is **stale/imprecise**, and in several cases asserts the **wrong licence** (WHG historically over-stamped everything `CC-BY-NC-4.0`, which is legally wrong over ODbL / ODC-By / CC-BY / public-domain sources).

The WHG registry now has **structured, push-managed** attribution fields. This handoff upgrades the ingestion side to populate them truthfully:

1. Migrate `AUTHORITIES` from the `citation` blob to **structured keys**.
2. Teach the push to emit them.
3. **Audit and correct every authority's real upstream licence** (research work — the existing blobs cannot be trusted).
4. Do a full, all-namespace reconciliation push (dry-run → diff → live) to **both prod and dev**.

---

## 1. The WHG endpoint contract (already live — do not change WHG)

`POST {WHG_INVENTORY_ENDPOINT}` (prod `https://whgazetteer.org/api/registry/inventory`; dev mirror `WHG_DEV_INVENTORY_ENDPOINT`). Bearer/token auth as today. Body is `{"gazetteers": [ <entry>, ... ]}`.

Each entry keeps its current keys (`id`, `name`, `description`, `namespace`, `class`, `owner_user_id`, `record_count`, `status`, `h3_coverage`, `temporal_extent`) and **may now also include** these optional Phase 4 fields:

| Payload key      | Type            | WHG behaviour |
|------------------|-----------------|---------------|
| `citation_text`  | string          | Human-readable citation. WHG's `attribution_for()` prefers this over `description`. |
| `license_spdx`   | string          | Resolved to a `License` FK by SPDX id. **Unknown codes are logged and skipped** (the rest of the entry still upserts) — never fatal. |
| `license_url`    | string (URL)    | Per-source deed override; falls back to the canonical `License.url` when null. |
| `rights_holder`  | string          | e.g. "J. Paul Getty Trust". |
| `source_url`     | string (URL)    | Homepage / landing page. |
| `contributors`   | list of objects | CRediT-shaped `[{"name","role","orcid"}]`; stored as `contributors_csl`. Optional. |

**Two contract guarantees that make this low-risk:**

- **Only keys present in the payload are written.** Omitting a field leaves any existing registry value intact. So a partial or staged rollout is safe.
- **Unknown payload keys are ignored** and **unknown `license_spdx` is skipped** (logged, not rejected). So pushing the upgraded payload to a prod that hasn't yet received the Phase 4 code is harmless — prod simply won't store the extras until parity lands. **You do not need to wait for prod parity to start sending the new fields.**

### 1.1 Licence SPDX ids seeded on WHG

`license_spdx` must match a seeded `licensing.License` row, or it's skipped. Currently seeded:

```
CC0-1.0   CC-BY-3.0   CC-BY-4.0   CC-BY-NC-4.0   CC-BY-SA-4.0
ODbL-1.0  ODC-By-1.0  custom-public-domain
```

If the audit (§3) turns up a licence **not** in this set (e.g. `CC-BY-NC-SA-4.0`, `CC-BY-2.5`), there are two options:
- **Add a seed row on WHG** — a one-line addition to `whg3/licensing/migrations/0002_seed_licenses.py`'s data list (or a new data migration). Coordinate this with the WHG repo; it's a trivial WHG change. **Preferred** for genuine SPDX licences.
- **Fall back** to the closest correct seeded row (or `custom-public-domain` for PD) **plus** a `license_url` override and a note. Use only for bespoke/non-SPDX terms.

Record any needed-but-unseeded SPDX ids in §3's table so the WHG seed can be extended in one pass.

---

## 2. Task 1 — Upgrade the ingestion pathway (mechanical)

### 2.1 Restructure each `AUTHORITIES` entry (`processing/settings.py`)

For every authority, **add** the structured keys alongside the existing `citation` (keep `citation` as a back-compat alias during transition — nothing else reads it yet, but leave it until the push is confirmed):

```python
{
    'dataset_name': 'TGN',
    'namespace': 'tgn',
    # NEW structured keys:
    'citation_text': 'The Getty Thesaurus of Geographic Names® (TGN), J. Paul Getty Trust.',
    'license_spdx':  'ODC-By-1.0',
    'license_url':   'https://opendatacommons.org/licenses/by/1-0/',
    'rights_holder': 'J. Paul Getty Trust',
    'source_url':    'https://www.getty.edu/research/tools/vocabularies/tgn/',
    'contributors':  [],   # optional CRediT list where the source documents roles
    # legacy (leave during transition):
    'citation': 'The Getty Thesaurus of Geographic Names® (TGN) is provided by ...',
    'api_item': '...',
    'files': [ ... ],
}
```

All new keys are **optional** — supply whatever the source documents (design decision: metadata flexibility). At minimum aim for `citation_text` + `license_spdx` + `rights_holder` + `source_url` per authority.

### 2.2 Extend the metadata lookup + payload builders (`processing/push_gazetteer_inventory.py`)

`_authority_meta(namespace)` currently returns only `{"name", "description"}`. Extend it to surface the new keys, e.g.:

```python
def _authority_meta(namespace: str) -> dict[str, Any]:
    for auth in AUTHORITIES:
        if auth.get("namespace") == namespace:
            return {
                "name": auth.get("dataset_name") or namespace.upper(),
                # description stays = the legacy blob for back-compat / prose;
                # citation_text is the structured human citation.
                "description": auth.get("citation"),
                "citation_text": auth.get("citation_text"),
                "license_spdx": auth.get("license_spdx"),
                "license_url": auth.get("license_url"),
                "rights_holder": auth.get("rights_holder"),
                "source_url": auth.get("source_url"),
                "contributors": auth.get("contributors") or [],
            }
    return {"name": namespace.upper(), "description": None}
```

Then merge the new keys into the entry dict in **both** builders — `build_inventory_payload()` (~line 290) and `build_single_authority_entry()` (~line 327). A small helper avoids drift:

```python
def _attribution_fields(meta: dict[str, Any]) -> dict[str, Any]:
    """Only include keys that are actually set (the endpoint leaves omitted
    fields untouched, so don't send nulls that would clobber curated values)."""
    out = {}
    for k in ("citation_text", "license_spdx", "license_url",
              "rights_holder", "source_url"):
        if meta.get(k):
            out[k] = meta[k]
    if meta.get("contributors"):
        out["contributors"] = meta["contributors"]
    return out
```

…and in each builder: `entry.update(_attribution_fields(meta))` before appending. (The `whg`-dataset fan-out in `_expand_whg_dataset_entries()` is per-Dataset, not authority-sourced — leave it untouched; dataset attribution comes from the contributor workflow, not `AUTHORITIES`.)

### 2.3 Update the module docstring payload example

Reflect the new optional keys in the `build_inventory_payload` docstring example so the contract is self-documenting (mirror the table in §1).

---

## 3. Task 2 — Audit + correct every authority's real licence (RESEARCH WORK)

**This is the substantive part and must not be done mechanically.** The existing `citation` blobs are stale and several licences are wrong. For **each** namespace below, verify against the source's current site/terms and fill in: SPDX id (or `custom`), deed URL, rights holder, a clean citation, and any documented contributor roles.

**AUDIT COMPLETE (2026-06-06).** The table below is now VERIFIED against each
source's official terms/repo/deposit (web research + primary-source fetch) and
applied to `processing/settings.py`. Confidence is `high` unless noted. Several
first-pass assumptions were WRONG — flagged in notes.

| ns    | name                  | `license_spdx`          | rights_holder                     | conf. | notes |
|-------|-----------------------|-------------------------|-----------------------------------|-------|-------|
| pl    | Pleiades              | `CC-BY-3.0`             | ISAW (NYU) & AWMC (UNC)           | high  | confirmed still **3.0** not 4.0 — pleiades.stoa.org/credits |
| gn    | GeoNames              | `CC-BY-4.0`             | Unxos GmbH                        | high  | geonames.org/about.html |
| tgn   | TGN                   | `ODC-By-1.0`            | J. Paul Getty Trust               | high  | getty.edu obtain-vocabularies |
| wd    | Wikidata              | `CC0-1.0`               | Wikimedia Foundation              | high  | data namespaces are CC0 |
| osm   | OSM                   | `ODbL-1.0`              | OpenStreetMap contributors        | high  | data is ODbL |
| ohm   | OHM                   | `CC0-1.0`               | OpenHistoricalMap contributors    | high  | **WRONG first-pass: NOT ODbL** — OHM is CC0 PD dedication |
| dp    | D-PLACE               | `CC-BY-NC-4.0`          | MPI for Evolutionary Anthropology | high  | **WRONG first-pass: NC**, not plain CC-BY; also cite upstream datasets |
| un    | ~~ISO3166 / Natural Earth~~ → UN Geospatial BNDA | ~~`custom-public-domain`~~ → **`custom-un-geodata`** | ~~Natural Earth~~ → United Nations | high | **SOURCE CHANGED 2026-07-14** (Natural Earth retired). The BNDA item carries NO grant of rights — only the boundary-designation disclaimer — so it is **not** public domain. Registry still holds the stale NE value; see the 2026-07-29 status note below |
| po    | PeriodO               | `CC0-1.0`               | PeriodO contributors              | high  | perio.do/license |
| gb    | GB1900                | `CC-BY-SA-4.0`          | GB Historical GIS + GB1900 partners | med | **WRONG first-pass: not CC0** — the abridged gazetteer (~1.17M = our count) is BY-SA; only the raw dump is CC0. SA deed version unstated; 4.0 assumed |
| nl    | NativeLand            | **custom (NO SPDX)**    | Native Land Digital               | high  | Data Sovereignty Treaty (OCAP®): NON-COMMERCIAL + redistribution-by-permission → **custom WHG License row** |
| ukhc  | UK Historic Counties  | **custom (NO SPDX)**    | Historic Counties Trust           | high  | bespoke permissive (commercial OK), attribution requested → **custom WHG License row** |
| iv    | Index Villaris (1680) | `CC-BY-SA-4.0`          | Gadd & Litvine (1680 src is PD)   | high  | repo LICENSE via GitHub API; 1680 John Adams source is PD |
| clio  | Cliopatria (Seshat)   | `CC-BY-4.0`             | Seshat: Global History Databank   | high  | repo LICENSE.md |
| chgis | CHGIS / TGAZ          | **custom (NO SPDX)**    | Harvard (Fairbank) & Fudan        | high  | academic-research-only: NO commercial use, resale, OR **redistribution** → **custom WHG License row** + possibly direct permission |
| dgsd  | DGSD                  | `CC-BY-ND-4.0` ⚠UNSEEDED | Ruth Mostern & Elijah Meeks      | high  | **WRONG first-pass: ND** (D-Scholarship badge), not NC-SA. ND not a blocker — Mostern is **WHG's PI** and endorses use. Needs seeding |
| tm    | Trismegistos          | `CC-BY-SA-4.0`          | Trismegistos / KU Leuven          | high  | **NOT non-commercial** (contrary to first-pass fear); trismegistos.org/dataservices |
| ofs   | Ottoman NFS Gazetteer | `CC-BY-4.0`             | Kabadayı, Sefer, Boykov & Gerrits | high  | Zenodo 7351936 Rights field |
| og    | Ottoman Gazetteer     | `CC-BY-NC-4.0`          | Will Hanley (FSU)                 | high  | repo LICENSE + README badge |
| kain_par | Ancient Parishes (Kain & Oliver / CAMPOP) | **custom (NO SPDX)** → `custom-ukds-eul` | Cambridge Group (CAMPOP); Kain & Oliver | high | UK Data Service End User Licence: registration-gated, research/teaching only, **no** commercial use, **no** onward distribution. Added to the table 2026-07-29 — it post-dates the original audit and had been sending `license_spdx: ''` (silently filtered as falsy) |
| loc   | LOC                   | — (relations-only)      | —                                 | n/a   | excluded from inventory; no registry row |

> **Needed-but-unseeded SPDX ids found during audit: `CC-BY-ND-4.0`** (dgsd only).
> Seed it on WHG (one-line addition to `licensing/migrations/0002_seed_licenses.py`).
> This is the ONLY unseeded *SPDX* — the audit's other gaps (nl, ukhc, chgis) are
> bespoke **non-SPDX** terms needing custom `License` rows, not seed additions.

> **Custom `License` rows — DONE on WHG atlas/dev (2026-06-06, licensing/0003).**
> The three bespoke sources now bind to custom `License` rows via these
> `license_spdx` lookup keys (set in `AUTHORITIES`):
> - **nl** → `custom-nativeland-dst` (commercial=False, custom=True)
> - **ukhc** → `custom-historic-counties` (commercial=True, custom=True)
> - **chgis** → `custom-chgis-academic` (commercial=False, custom=True)
>
> Plus `CC-BY-ND-4.0` seeded (dgsd). All four resolve on **dev**. **Prod parity
> lands at the atlas→main promotion** — until then a prod push logs "unknown
> license_spdx" + skips the FK for these four (the other fields still upsert;
> self-corrects with a re-push after promotion). The other 16 authorities resolve
> on prod today.

> **⚠ STATUS 2026-07-29 (place#157) — still unresolved on BOTH stacks.** The
> paragraph above is stale: `GET /api/attribution/` reports `license: null` for
> `nl`, `ukhc`, `chgis`, `dgsd` **and** `kain_par` on prod *and* on dev, so
> whatever `licensing/0003` did has not reached either running site — or it has,
> and no inventory push has landed since to set the FK. Two further findings:
>
> - **`un` is not just missing, it is WRONG.** The registry still holds
>   `custom-public-domain` — the Natural Earth determination — while the source
>   has been UN Geospatial BNDA (© United Nations, *no* public-domain grant)
>   since 2026-07-14. Our `custom-un-geodata` was skipped as unknown, and a skip
>   leaves the stale row standing rather than clearing it. **A silent skip can
>   preserve an affirmatively false licence, not merely an absent one.**
> - **`kain_par` was never sending anything** — it carried `license_spdx: ''`,
>   which `_attribution_fields` filters out as falsy. Now
>   `custom-ukds-eul` (defined in `settings.CUSTOM_LICENCES`).
>
> The whole class of failure is invisible because the endpoint skips-and-logs
> WHG-side. It is now checkable in one command from this repo:
> `python -m processing.verify_licences` (exit ≠ 0 on any unresolved licence)
> and `--seed-json` emits the exact custom `License` rows to create.
> `push_gazetteer_inventory` runs the same check after every push and warns.
>
> **Remaining work is whg3-side:** seed/deploy the five rows
> (`custom-nativeland-dst`, `custom-historic-counties`, `custom-chgis-academic`,
> `custom-ukds-eul`, `custom-un-geodata`) plus the SPDX row `CC-BY-ND-4.0`, then
> re-push the inventory and re-run the verifier.

Bespoke / non-SPDX terms → use `custom-public-domain` only if truly PD; otherwise flag for a `custom=True` WHG `License` row + a free-text rights statement (coordinate with the WHG repo).

---

## 4. Task 3 — All-namespace reconciliation push

Push **all** namespaces (a full reconciliation, not just changed ones) so every `GazetteerRegistryEntry` is refreshed against the new contract. Both prod and dev are first-class targets (the push already mirrors to both by default — see the `("prod", …), ("dev", …)` target loop ~line 586).

```bash
# 1. DRY RUN — inspect the full payload, confirm the new keys are present
python -m processing.push_gazetteer_inventory --run-id <RUN_ID> --dry-run \
  | python -m json.tool | less

# 2. Diff review — compare what each registry row WOULD become against current.
#    (Eyeball the dry-run JSON; spot-check 3-4 namespaces incl. an ODbL one
#    and a PD one to confirm license_spdx + rights_holder are correct.)

# 3. LIVE — pushes prod AND the dev mirror (omit --no-dev-push to hit both)
python -m processing.push_gazetteer_inventory --run-id <RUN_ID> \
  --auth-token-file ~/.whg/inventory.token

# Prod-only (if you must stage): add --no-dev-push
# Dev-only smoke test first: --endpoint <dev-url> --no-dev-push  (or set
#   --dev-endpoint and an empty --endpoint is NOT supported — use --endpoint
#   pointed at dev for a dev-only trial)
```

**Recommended sequence:** dry-run → push **dev only** first and eyeball the WHG dev Gazetteers offcanvas + `GET /api/attribution/?namespaces=tgn,osm` (as a superuser, since dev gates anon) → then the full prod+dev push.

### 4.1 Verify on the WHG side after the push

- Dev attribution endpoint (superuser/bearer on dev): `GET /api/attribution/?namespaces=tgn,osm,wd` should now show `license: {spdx_id, label, url}`, `rights_holder`, and `citation` sourced from `citation_text`.
- Django admin → API → Gazetteer registry entries: the "Attribution / licence / rights" fieldset should be populated (and will now be **overwritten** by each push — it became the source of truth the moment the push started sending these fields).
- Unknown-SPDX warnings appear in the WHG `indexing` logger if any `license_spdx` didn't resolve — chase those down (seed the licence or fix the code).

---

## 5. Definition of done

- [x] Every `AUTHORITIES` entry (except relations-only `loc`) has `citation_text`, `license_spdx`, `rights_holder`, `source_url` set, with licences **verified against the live source**, not copied from the old blob. (commit 0b750d1 + 0f27f13)
- [x] Any needed-but-unseeded SPDX licences listed + added to the WHG seed: `CC-BY-ND-4.0` + custom rows `custom-nativeland-dst` / `custom-historic-counties` / `custom-chgis-academic` (WHG licensing/0003, atlas/dev).
- [x] `_authority_meta` + both payload builders emit the new fields; module docstring updated.
- [x] Dry-run reviewed; dev push done + **verified via the attribution endpoint** (all 19 authorities incl. the 4 custom/ND resolve their full license block, no unknown-SPDX errors); full prod+dev push done (HTTP 200, 23 + 3×1 upserted, no errors).
- [x] No authority still implicitly carries the wrong `CC-BY-NC-4.0` assertion.

### ⚠ Remaining: prod re-push after the atlas→main promotion

**Pushed 2026-06-06** via `postbarrier-20260502T130000Z` (full run, 16 authorities
+ whg) + single-namespace pushes for `ofs`/`og`/`ukhc` (not in that run inventory),
to **prod and dev**, with `--skip-tileserver-check` (reconciliation of already-served
gazetteers).

**Dev = fully live and verified.** **Prod accepted the push but stores none of the
new attribution fields yet** — prod's WHG code is pre-Phase-4, so per the §1
forward-compat guarantee it kept the legacy `citation` blob and ignored
`citation_text`/`license_spdx`/`rights_holder`/`source_url` for **all 19**
authorities (not just the 4 new-licence ones). This is harmless and expected.

→ **After the atlas→main promotion lands the Phase-4 code + licensing/0003 on prod,
re-run the identical push** to populate prod:
```bash
# on crc0 (native stg135 uid reads the secrets tokens)
python -m processing.push_gazetteer_inventory --run-id postbarrier-20260502T130000Z --skip-tileserver-check
for ns in ofs og ukhc; do python -m processing.push_gazetteer_inventory --namespace $ns --skip-tileserver-check; done
# verify: GET https://whgazetteer.org/api/attribution/?namespaces=osm,dgsd,nl  (browser UA; the GET is bot-gated)
```

## 6. Out of scope (WHG-side, already done or separate)

- The registry schema + endpoint + `attribution_for()` + admin fieldset (WHG Phase 4 — **done**, atlas/dev live).
- Surfacing attribution in the public place/search/download responses (WHG Phase 3/5 — separate).
- CRediT for contributed *datasets/collections* (WHG Phase 2 — done; this handoff only concerns *authority* contributors via the optional `contributors` key).
