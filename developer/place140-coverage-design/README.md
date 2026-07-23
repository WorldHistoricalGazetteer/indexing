# place#140 — Polygon-gazetteer coverage footprint (design + styling spec)

This folder documents the design that **replaces** the first-cut #140 heatmap
(centroid points) with a **dissolved coverage footprint** for polygon
gazetteers, and gives the **whg3 styling agent** an exact, copy-pasteable recipe.

See `reference-styling-prototype.html` for the working MapLibre reference that
produced the locked look (fetches the vob_rc footprint; run under any static
server against the live `tiles.whgazetteer.org` styles).

## Why the heatmap was wrong

The MapLibre `heatmap` layer consumes **Point** geometry only. For a polygon
gazetteer the injected centroid points render as a **scattered sparse dot
field**, which reads as "almost no coverage" — the very problem #140 set out to
fix (see the live `vob_rc` example). Experiments also showed the tiles already
retain **97–99% of the true areal coverage** at low zoom, so the sparseness was
never a tile-content problem — it was a **representation/styling** problem.

## The model: coverage footprint → boundaries

- **Low zoom (z0–7):** one **dissolved** (unary_union) footprint polygon per
  gazetteer, styled as a warm **mottled "heat-like" fill** — this is the polygon
  analogue of the point heatmap: *"this gazetteer covers this region."*
- **z8+:** the footprint hands off to the **real discrete boundaries** (thin red
  outlines) — *"here are the individual features."*

The two use a deliberately different visual language so the reader always knows
which they're seeing.

## Design evolution (traces the exploration)

1. `01-first-trials-neutral-palette.jpg` — first fill/edge trials (placeholder colours)
2. `02-heatmap-palette-real-basemap.jpg` — recoloured to the heatmap ramp on the real whg-context basemap
3. `03-cross-stipple-dotted-glow.jpg` — cross-sprite stipple + dotted glow edge
4. `04-two-colour-offset-sprite.jpg` — two offset sprite layers, alpha-mixed
5. `05-jittered-soft-blob-mottle.jpg` — soft radial blobs on a jittered grid → organic mottle
6. `06-per-colour-blob-sizing.jpg` — different blob sizes per colour to enrich the mottle
7. `07-blue-orange-red-rejected.jpg` — full-range blue variant (rejected — muddy overlaps)
8. `08-candidates-A-vs-B-across-zoom.jpg` — uniform (A) vs per-colour-sized (B) across zoom
9. `09-zoom-blend-attempt.jpg` — zoom-interpolated A→B (dropped in favour of a fixed recipe)
10. `10-reduced-opacity-over-osm.jpg` — reduced opacity over OSM (drove the global-0.7 decision)
11. `11-locked-B-both-basemaps.jpg` — **locked recipe** across z0–z8 on both basemaps

## Locked recipe (candidate B)

- **Fill** = three `fill-pattern` layers, one per colour, each a seamless 72 px
  tile of **soft radial "blobs" on a jittered grid** (blob Ø > spacing so they
  overlap and alpha-blend into an organic mottle). Different blob size per colour
  (big warm base → small hot cores):

  | colour | rgb | blob radius | grid (g×g) | jitter | base opacity | seed |
  |--------|-----|------------:|-----------:|-------:|-------------:|-----:|
  | RED    | 224,64,64  | 24 | 3 | 9 | 0.45 | 101 |
  | ORANGE | 253,141,60 | 16 | 4 | 7 | 0.50 | 202 |
  | YELLOW | 255,237,160| 10 | 6 | 5 | 0.60 | 303 |

  Layer order bottom→top: **RED, ORANGE, YELLOW**.
- **Overall opacity = 0.7 GLOBALLY** (every base opacity above is multiplied by
  0.7). Never 1.0 on any basemap — there must always be some basemap
  bleed-through.
- **Border** = solid red line, `line-width 1.5`, **no blur, no dash**,
  `line-opacity 0.8`.
- **Seeded (deterministic) scatter** so the mottle is stable across sessions.
- Footprint layers are `maxzoom 8`; boundaries appear from `minzoom 8` (enforced
  by the tiles — see the indexing side).

## whg3 implementation instructions

The mottle is **client-side styling** (sprites generated at runtime + a
`fill-pattern`). The **tiles carry only geometry**: a single dissolved polygon
tagged `coverage:1` (present z0–7) plus the real boundaries (present z8+), all in
the one gazetteer source-layer. So whg3 needs to:

**1. Generate the three sprite images once (per map), add coverage layers, add a border.**

```js
// --- place#140 coverage footprint styling (add inside loadGazetteerStyle) ---
const COV_MAXZOOM = 8, OVERALL = 0.7;
const _C = { RED:[224,64,64], ORANGE:[253,141,60], YELLOW:[255,237,160] };
const _rgba = (c,a)=>`rgba(${c[0]},${c[1]},${c[2]},${a})`;
// [key, color, radius, grid, jitter, baseOpacity, seed]  (bottom→top)
const COV_LAYERS = [
  ['red',    _C.RED,    24, 3, 9, 0.45, 101],
  ['orange', _C.ORANGE, 16, 4, 7, 0.50, 202],
  ['yellow', _C.YELLOW, 10, 6, 5, 0.60, 303],
];
function _covSprite(color, radius, grid, jit, seed){
  const TILE=72; let s=seed>>>0; const rnd=()=>{ s=(s*1664525+1013904223)>>>0; return s/4294967296; };
  const cv=document.createElement('canvas'); cv.width=cv.height=TILE;
  const x=cv.getContext('2d'), cell=TILE/grid, pts=[];
  for(let i=0;i<grid;i++) for(let j=0;j<grid;j++)
    pts.push([(i+0.5)*cell+(rnd()*2-1)*jit, (j+0.5)*cell+(rnd()*2-1)*jit]);
  pts.forEach(([px,py])=>{ for(let dx=-1;dx<=1;dx++) for(let dy=-1;dy<=1;dy++){
    const gx=px+dx*TILE, gy=py+dy*TILE, g=x.createRadialGradient(gx,gy,0,gx,gy,radius);
    g.addColorStop(0,_rgba(color,1)); g.addColorStop(1,_rgba(color,0));
    x.fillStyle=g; x.beginPath(); x.arc(gx,gy,radius,0,7); x.fill();
  }});
  return x.getImageData(0,0,TILE,TILE);
}

// register sprite images once (shared across source-layers)
for (const [key,color,r,g,j,,seed] of COV_LAYERS){
  const imgId = `whg_cov_${key}`;
  if (!this.hasImage(imgId)) this.addImage(imgId, _covSprite(color,r,g,j,seed), {pixelRatio:2});
}

// ...inside the per-vector-layer loop, using the same `baseId`/`sourceLayer`/`beforeId`:
for (const [key,,,,,op] of COV_LAYERS){
  const id = `${baseId}_coverage_${key}`;
  if (!this.getLayer(id)) this.addLayer({
    id, type:'fill', source:id_of_source, 'source-layer':sourceLayer, maxzoom:COV_MAXZOOM,
    filter:['==',['get','coverage'],1],
    paint:{ 'fill-pattern':`whg_cov_${key}`, 'fill-opacity': op*OVERALL },
  }, beforeId);
}
if (!this.getLayer(`${baseId}_coverage_line`)) this.addLayer({
  id:`${baseId}_coverage_line`, type:'line', source:id_of_source, 'source-layer':sourceLayer, maxzoom:COV_MAXZOOM,
  filter:['==',['get','coverage'],1],
  paint:{ 'line-color':_rgba(_C.RED,1), 'line-width':1.5, 'line-opacity':0.8 },
}, beforeId);
```

**2. Exclude the coverage feature from the existing polygon/heat layers** so they
don't double-draw it and so it never becomes clickable:

```js
// _fill  → add the coverage exclusion:
filter: ['all', ['==',['geometry-type'],'Polygon'], ['!',['has','coverage']]]
// _line  →
filter: ['all', ['match',['geometry-type'],['Polygon','LineString'],true,false], ['!',['has','coverage']]]
// _heat  → (defensive; coverage carries no Point geometry anyway)
filter: ['all', ['==',['geometry-type'],'Point'], ['!',['has','coverage']]]
```

**3. Popups / clicks.** The coverage feature has **no `place_id`** and its layers
(`_coverage_*`, `_coverage_line`) are **not** in `gazetteerInteraction`'s
`SHAPE_SUFFIXES` (`_fill`/`_line`/`_circle`), so it is unclickable by
construction. As belt-and-braces, also skip any queried feature lacking a
`place_id` in the click handler.

**4. Point gazetteers are unaffected** — they carry no `coverage` feature, so the
`_coverage_*` layers render nothing and the existing `_heat`/`_circle` behaviour
is unchanged. No per-gazetteer branching needed.

### Result
- z0–7: warm mottled coverage footprint (fill ×0.7 + solid red border), fading
  out by z8.
- z8+: real discrete boundaries (existing `_fill`/`_line`) in red.
- Consistent across `whg-context` and `OSM` basemaps (0.7 keeps detail bleeding
  through); see `11-locked-B-both-basemaps.jpg`.
