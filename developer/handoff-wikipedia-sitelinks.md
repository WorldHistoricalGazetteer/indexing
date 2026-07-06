# Handoff — surface Wikipedia links from Wikidata sitelinks into the index

**Status:** ready for a coding agent on the `indexing` repo. Requested by Stephen 2026-07-06.
**Origin:** the Gazetteer Workbench (WHG `website` repo) wants to enrich reconciled places with
Wikipedia links. The natural source is the Wikidata `sitelinks` already present in the dump we ingest,
but they are currently discarded. This benefits the whole platform, not just the Workbench.

## The gap (verified)
- We build Wikidata place docs in **`authorities/wikidata-places.py` → `create_place_doc_fast`**
  (around L222–348). It reads `labels`, `aliases`, `claims` (coords, P297 ccodes, P31 types, P1566
  GeoNames relation, etc.) but **never reads `entity['sitelinks']`.**
- The source **`latest-all.json.gz` dump already contains sitelinks per entity** — each geographic
  entity carries `entity["sitelinks"] = { "enwiki": {"site":"enwiki","title":"London","badges":[…]},
  "frwiki": {...}, … }`. The raw material is right there in the object we're already iterating.
- The **staged place schema already has a `links` nested field** (`schemas/places.json` L197–206:
  `{ type: keyword, identifier: keyword }`), which flows through the pipeline into the index and is
  already surfaced by the website's `GET /entity/{id}/api`. So there is a ready home for the data.
- Confirmed there is **no existing sitelink/enwiki/wikipedia handling** anywhere in this repo's ingest
  path (`grep -ri sitelink authorities processing` → nothing in the wd path).

## The task
In `create_place_doc_fast`, extract `entity.get('sitelinks', {})` and append Wikipedia links to
`doc['links']` (create the list if absent — today `links` is only set for… actually it isn't set at
all for wd; add it). Recommended shape, matching the existing `links` contract:

```python
# after the existing optional-field extraction, before `return doc, geoshape_ref`
sitelinks = entity.get('sitelinks') or {}
wiki_links = []
for site, sl in sitelinks.items():
    if not site.endswith('wiki'):        # skip wikiquote/wikivoyage/commons/etc. unless wanted
        continue
    lang = site[:-4]                     # 'enwiki' -> 'en'
    title = sl.get('title')
    if not lang or not title:
        continue
    url = f"https://{lang}.wikipedia.org/wiki/" + quote(title.replace(' ', '_'))
    wiki_links.append({'type': 'seeAlso', 'identifier': url})
if wiki_links:
    doc.setdefault('links', []).extend(wiki_links)
```

(`from urllib.parse import quote` at module top.) Notes / decisions for the implementer:

- **`type` value:** `seeAlso` is the safest LPF/SKOS-neutral choice for a Wikipedia article URL (it is
  NOT a `closeMatch`/`exactMatch` — those are reserved for authority co-references). If a richer
  vocabulary is preferred, `primaryTopicOf` is defensible. Pick one and be consistent.
- **Language policy / index size:** a well-known place can have 100–300 sitelinks; storing every URL
  bloats the index. **Recommended default: keep `enwiki` plus a small configured allow-list** (e.g.
  the WHG UI languages) — make it a module constant so it's easy to tune. Storing only the `title`
  (not a full URL) and building the URL at read time is an alternative if you'd rather keep it compact,
  but that requires the website side to know the base-URL convention; a stored URL is self-describing.
- **Dedup:** if you also add authority `links` later, dedupe by `identifier`.
- **Keep it cheap:** this is in the hot per-entity loop over the whole dump — the code above is O(sitelinks) and allocation-light; avoid regex.

## Verification
1. Stage a slice and inspect: run the wd staging over a small sample (or grep the dump for a known QID —
   **Q84 = London**, **Q90 = Paris**) and confirm `places.jsonl` records now carry
   `links: [{type:"seeAlso", identifier:"https://en.wikipedia.org/wiki/London"}, …]`.
2. Reindex (or apply as an update patch if a full wd rebuild is too costly — see
   `processing/apply_update_patch.py`; `links` would need to be an allowed patch field).
3. Query the live index / gateway for a wd place and confirm the `links` come back.

## Companion change (separate task, `website` repo — for whoever picks this up next)
Populating the index is necessary but not sufficient — the website currently **strips** links from
reconciliation candidates. To make Wikipedia links show up when WD candidates are returned by
`/reconcile` (Stephen's actual goal), a follow-up in the **`website` (whg3)** repo must surface them:
- `api/reconcile_helpers.py` → `make_candidate` (~L204–232) does not read `links`; add the wd `links`
  (or just the Wikipedia ones) to the candidate payload.
- The CRC gateway adapter `api/crc_client.py` (~L462–469) drops `links` before `make_candidate` — stop
  dropping them.
- `GET /entity/{id}/api` (`api/views_entity.py`) already emits `links`; once the index carries the
  Wikipedia entries they will appear there automatically.

Full context and the two-option analysis (this server-side route vs. a browser Wikidata-API fallback)
is in the website repo at `developer/plan-workbench-wikipedia-enrichment.prompt.md`.

## References
- `authorities/wikidata-places.py` (`create_place_doc_fast` L222–348, `stage_wikidata` L351+)
- `schemas/places.json` (`links` L197–206)
- Dump: `${DATA_DIR}/wikidata/latest-all/latest-all.json.gz`
