# Plan — Parish (& admin) boundary extraction from OS six-inch maps (VLM/CV)

> **Status:** Design / experiment. Grounded in the real raster 2026-07-17. A
> follow-on to GB-STAMP (`plan-gb1900-typing.md`); shares its tile cache + VLM infra.
> **Goal:** derive **historical admin boundaries (esp. civil parishes) c.1900** by
> extracting them from the OS six-inch 2nd-ed raster itself — a **fully-open, our-own
> derivation** — to label each GB-STAMP point with its admin hierarchy, without the
> licence problems of CAMPOP (safeguarded) or GBHGIS (CC-BY-SA/commercial).

## Scope — ALL of parish, district, borough AND county (our own, co-registered)
This project extracts the **full admin hierarchy** off the six-inch raster: **civil
parishes, rural/urban districts, municipal/parliamentary boroughs, AND counties**. We
want **our own** boundaries for every level — even county, which *is* available
elsewhere (Historic Counties Trust / `ukhc`) — because a county line traced from the
**same raster, by the same pipeline** as the parishes and districts it contains is
**precisely co-registered** with them: parishes nest cleanly inside their district,
district inside county, with no cross-source sliver/gap artefacts. A borrowed county
polygon from a different survey/generalisation would not align pixel-for-pixel with our
extracted sub-units. Co-registration is the point.

## Why this route
- **County** is available open elsewhere (Historic Counties Trust / `ukhc`) and **modern**
  admin is open (OS Boundary-Line, OGL) — but those are *other people's geometries* on
  *other* surveys; we want county lines drawn from **our** raster so they nest exactly
  with our parishes/districts (see Scope). The genuinely un-sourced gap is **historical
  (c.1900) sub-county** admin — parishes, rural districts, unions — where the only rich
  source (**CAMPOP 1851**) is **safeguarded, non-redistributable**
  (see `markets/geodata/1851/LICENCE.md`).
- The OS six-inch sheets **draw these boundaries and label their type**. Extracting
  them ourselves off the NLS raster (which we're licensed to read for research) yields
  an **openly-publishable** admin layer. CAMPOP is then used only as **internal,
  registered-access ground truth for validation** — never republished.

## Grounding — what the raster actually shows (verified 2026-07-17)
Stitched OS six-inch near Dolgarrog (Conwy valley). Direct observations:
- **Admin boundaries are rendered distinctively** — a "pecked" line of **× marks and
  dots** ("mereing" symbols showing what the boundary follows), clearly **different
  from the thin solid field/enclosure lines**.
- **Boundary TYPE is inline-labelled and legible**: `Union & R.D. By.` (Union & Rural
  District Boundary), `Co.` (County), `C.P.` would mark a Civil Parish. Also `HW.M.O.T`
  (tidal), `B.M.` (bench mark). These follow the documented OS 404 boundary/mereing
  conventions we already hold (`gb1900_os_lettering.json` era).
- Boundaries **follow features** (rivers, roads, fences) — so their geometry ≈ the
  underlying feature's line + the × mereing marks.
- **Feasibility signal:** a capable VLM can *identify and read* these boundaries + type
  labels directly (confirmed by human view). The hard part is **precise geometry**, not
  recognition.

## Capitalise on the GB1900 transcriptions we ALREADY have (big shortcut)

The volunteers already transcribed the **boundary labels**, georeferenced to points —
so the *semantic* half (boundary TYPE + which units) is largely free from GB-STAMP
data itself; the VLM only fills gaps. Mined the 2.67M labels (2026-07-17):

| Boundary type | GB1900 labels | example |
|---|---|---|
| Rural/Urban **District** (`R.D./U.D. By.`) | **19,038** | `U.D. By.`, `Union & R.D. By.` |
| **Borough** (`Munl./Parly. Boro. By.`) | **4,401** | `Parly. Boro. By.` |
| **County** (`Parly. Co. By.`) | **710** | `Parly. Co. By. (Carn.) (Denb.)` ← names *both* counties |
| **Detached** parts (`(Det.)`) | 321 | `DOLGARROG (Det.)` |
| **Civil parish** (`C.P.`) | **only 57** | `C.P.` |

**Implications:**
- **District / borough / county boundaries are richly pre-labelled** — these georef'd
  points give us both *where* a boundary runs and *what type* (and, for counties, the
  pair it divides), which **bootstraps the CV detection + classification** for those
  levels. But we still trace the *geometry* ourselves off the raster (the labels are
  points, not lines) so that district/borough/**county** polygons are **co-registered**
  with the parishes — see Scope. All three are wanted deliverables, not throwaway.
- **Civil parishes are barely labelled (57 `C.P.`)** — the six-inch labels the parish
  line sparsely. So parish extraction specifically **cannot lean on the transcriptions**;
  it needs CV line-detection + naming from *enclosed GB-STAMP settlement labels* (and
  the many parish boundaries that coincide with district/county lines). Parishes are
  **the hardest and most essential** level; districts/boroughs/counties are easier
  (label-bootstrapped) but equally in-scope, extracted by the same pipeline for exact
  nesting.

  (Hundreds/wapentakes are out of scope — not drawn on the c.1900 six-inch, and not
  relevant to this project.)

## Method — HYBRID (CV geometry + VLM semantics)
VLMs hallucinate coordinates, so don't ask a VLM to trace precise polylines. Split:

1. **Line detection / segmentation (CV)** — detect boundary pixels (the ×-dot pecked
   line) and vectorise to polylines. Options, cheapest first:
   - **MapReader** (Living with Machines; the NLS-map ML toolkit) — patch classification
     / segmentation purpose-built for OS six-inch — the natural first tool to try.
   - A small **semantic-segmentation** model fine-tuned on boundary vs not (weak labels
     from CAMPOP/OS-BL rasterised onto tiles — *internal training only*).
   - Classical CV fallback: the ×-dot mereing pattern is a distinctive texture (template
     / morphology) — cheap baseline to gauge separability from field lines.
2. **Semantic labelling (VLM)** — on crops around detected boundary segments and their
   inline labels, the VLM reads the **boundary TYPE** (`C.P.`/`R.D.`/`Co.`/`Union`) and
   any name — reusing the GB-STAMP VLM infra (h200, marker-crop + verbatim recipe).
3. **Assembly** — link classified segments into **closed polygons** per admin level
   (topology/graph closure; boundaries share the same feature lines).
4. **Attribution** — name each polygon from: enclosed GB-STAMP settlement labels, any
   `C.P.` label, and cross-reference to `ukhc` county + (internal) CAMPOP.
5. **Validation** — score extracted polygons against **CAMPOP 1851** (internal,
   registered-access) and OS Boundary-Line — IoU / boundary-recall. Publish only our
   own geometry (open).

## Reuse / cost
- **Tiles already cached** on `/vast/ishi/gb1900/tiles` (the GB-STAMP fetch) — no new
  NLS load. Crop/stitch tooling exists (`gb1900_tiles.py`). VLM infra proven (h200).
- The VLM semantic pass is small (only boundary crops, far fewer than 1M labels).
- The CV/segmentation is the research unknown → do a **detection probe first**.

## Phased pilot (start ASAP; schedule GPU around the running GB-STAMP job)
- **P0 — grounding (DONE):** confirmed boundaries are visible, distinct, type-labelled,
  VLM-legible.
- **P1 — detection probe (CPU + light GPU):** on a small county tile set, try MapReader
  (and a classical ×-dot texture baseline) to detect/vectorise boundary lines; measure
  separability from field lines. Decides the geometry method.
- **P2 — VLM semantic probe (spare GPU):** on ~a few hundred boundary crops, the VLM
  reads boundary TYPE + name. (Confirms the human-level read scales; runs on spare h200
  *after* / alongside the GB-STAMP run — do NOT contend with it.)
- **P3 — one-parish end-to-end:** detect → classify → close one parish polygon → name →
  score vs CAMPOP. Go/no-go on the whole approach.
- **P4 — scale** to a county, then GB; join polygons to GB-STAMP points (same
  point-in-polygon tooling as `gb1900_dating.py`, `--src-epsg 27700`).

## Licensing
Extracted boundaries are **our derivation off the NLS raster** → openly publishable
(CC0/CC-BY, attribute NLS for the source imagery, per §5.3 of the typing plan). CAMPOP
and GBHGIS are used **only as internal validation ground truth**, never redistributed.

## Practical note
The GB-STAMP typing run currently owns the h200 workers. P1 (CV/MapReader detection) is
independent of that GPU and can start now; the P2 VLM probe should wait for spare h200
capacity (or run after the typing run finishes) so it doesn't slow GB-STAMP.
