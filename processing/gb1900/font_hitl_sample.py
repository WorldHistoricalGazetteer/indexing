#!/usr/bin/env python
"""Build a stratified font-review sample for the HITL AAT-typing tool.

Reads the VLM outputs (os_style / case / size_band / bbox / vlm_text per label), stratifies
by `os_style` (× case), samples N per stratum, and for each makes a **tight bbox-crop** of the
label (the VLM bbox is fractional coords within the marker-crop). Emits a self-contained
`manifest.json`: per sample the base64 tight crop + the current `os_style → type-token`
mapping, for a human to validate/correct → crosswalk edits (never-re-run patch).

  python -m processing.gb1900.font_hitl_sample \
      --vlm-glob '/vast/ishi/gb1900/edition/vlm/*/shard-0.jsonl' \
      --crops /vast/ishi/gb1900/crops/national \
      --lettering typesystem/data/gb1900_os_lettering.json \
      --per-style 24 --out /vast/ishi/gb1900/probe/hitl/manifest.json
"""
from __future__ import annotations
import argparse, base64, glob, io, json, random, sys
from pathlib import Path

OS_STYLE_DESC = {
    "RP": "Roman Print (serif, mixed-case) — named buildings",
    "RC": "Roman Capitals — prominent settlements / admin places",
    "IC": "Italic Capitals — water & designed-water features",
    "EC": "Egyptian Capitals (slab) — ROMAN antiquities",
    "OldEnglish": "black-letter — pre-historic / Saxon antiquities",
    "GermanText": "black-letter — Norman / medieval antiquities",
    "Ornamental": "decorative — counties / county boroughs",
    "Stump": "plain stamped hand — everything else (minor features)",
    "illegible": "illegible",
}


def tight_crop_b64(crop_path: Path, bbox, out_h=64):
    from PIL import Image
    try:
        im = Image.open(crop_path).convert("RGB")
    except Exception:
        return None
    W, H = im.size
    if bbox and len(bbox) == 4:
        x0, y0, x1, y1 = bbox
        px = [max(0, x0 * W), max(0, y0 * H), min(W, x1 * W), min(H, y1 * H)]
        if px[2] - px[0] > 4 and px[3] - px[1] > 4:
            pad_x = (px[2] - px[0]) * 0.12; pad_y = (px[3] - px[1]) * 0.25
            im = im.crop((max(0, px[0]-pad_x), max(0, px[1]-pad_y),
                          min(W, px[2]+pad_x), min(H, px[3]+pad_y)))
    w, h = im.size
    if h > 0:
        im = im.resize((max(1, int(w * out_h / h)), out_h))
    buf = io.BytesIO(); im.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def run(a):
    style_map = {}
    if a.lettering:
        d = json.loads(Path(a.lettering).read_text(encoding="utf-8"))
        style_map = {k: v for k, v in (d.get("style_to_type_token") or {}).items()
                     if not k.startswith("_")}
    by_style: dict[str, list] = {}
    n = 0
    for fn in glob.glob(a.vlm_glob):
        for line in open(fn, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            v = r.get("vlm") or {}
            if not v.get("legible", True):
                continue
            st = v.get("os_style")
            if not st:
                continue
            by_style.setdefault(st, []).append({
                "pin_id": r.get("pin_id"), "os_style": st, "case": v.get("case"),
                "size_band": v.get("size_band"), "text": v.get("vlm_text"),
                "bbox": v.get("bbox"), "type_token": r.get("vlm_type_token")})
            n += 1
    print(f"[hitl] {n:,} legible VLM records across {len(by_style)} styles", flush=True)

    rng = random.Random(0)
    samples = []
    for st, items in sorted(by_style.items()):
        rng.shuffle(items)
        for it in items[: a.per_style]:
            b64 = tight_crop_b64(Path(a.crops) / f"gb_{it['pin_id']}.png", it.get("bbox"))
            if b64 is None:
                continue
            it["crop"] = b64
            it["style_desc"] = OS_STYLE_DESC.get(st, st)
            it["mapped_token"] = style_map.get(st)
            samples.append(it)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    manifest = {"styles": {s: OS_STYLE_DESC.get(s, s) for s in sorted(by_style)},
                "style_to_type_token": style_map,
                "counts": {s: len(v) for s, v in sorted(by_style.items())},
                "samples": samples}
    Path(a.out).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    print(f"[hitl] wrote {len(samples)} samples → {a.out} "
          f"({Path(a.out).stat().st_size/1e6:.1f} MB)")
    return manifest


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--vlm-glob", required=True)
    p.add_argument("--crops", required=True)
    p.add_argument("--lettering", default="typesystem/data/gb1900_os_lettering.json")
    p.add_argument("--per-style", type=int, default=24)
    p.add_argument("--out", required=True)
    run(p.parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
