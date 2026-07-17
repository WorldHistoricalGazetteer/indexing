#!/usr/bin/env python
"""Export the current GB-STAMP edition (so far) to CSV, with HCT county labels.

One streaming pass over the Tier-0 typed records (`national_typed.jsonl` — coords, name,
tier-0 type), merging the VLM re-reads produced so far (`vlm/batch_*/shard-0.jsonl` — VLM
text + os_style + type where the residual has been processed), and adding the historic
county (HCT HCS 3-char code) by point-in-polygon of the label CENTRE, with the near-border
uncertainty flag (see gb1900_county_attribution).

  python -m processing.gb1900_export_csv \
      --records /vast/ishi/gb1900/edition/national_typed.jsonl \
      --vlm-glob '/vast/ishi/gb1900/edition/vlm/batch_*/shard-0.jsonl' \
      --hct /vast/ishi/gb1900/probe/boundary/UKDefinitionA.shp \
      --out /vast/ishi/gb1900/edition/gb-stamp_so_far.csv
"""
from __future__ import annotations
import argparse, csv, glob, json, sys
from processing.gb1900_county_attribution import label_centre, load_hct


def load_vlm(pattern):
    """pin_id -> {vlm_text, os_style, type_token} from the VLM shards processed so far."""
    d = {}
    for fn in glob.glob(pattern):
        for line in open(fn, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            v = r.get("vlm") or {}
            d[r.get("pin_id")] = {"vlm_text": v.get("vlm_text"), "os_style": v.get("os_style"),
                                  "legible": v.get("legible"), "type_token": r.get("vlm_type_token")}
    return d


def run(a):
    from shapely.geometry import Point
    from shapely import prepared
    vlm = load_vlm(a.vlm_glob)
    print(f"[csv] loaded {len(vlm):,} VLM records so far")
    tree, geoms, codes = load_hct(a.hct)
    prep = [prepared.prep(g) for g in geoms]
    print(f"[csv] loaded {len(geoms)} HCT counties")
    cols = ["place_id", "pin_id", "lon", "lat", "name", "type_token", "type_method",
            "os_style", "legible", "hc_county", "hc_county_uncertain", "hc_county_border_m"]
    n = 0
    with open(a.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore"); w.writeheader()
        for line in open(a.records, encoding="utf-8"):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            n += 1
            pid = rec.get("place_id"); pin = rec.get("pin_id")
            t = rec.get("text"); name = t.get("value") if isinstance(t, dict) else t
            row = {"place_id": pid, "pin_id": pin, "lon": rec.get("lon"), "lat": rec.get("lat"),
                   "name": name}
            v = vlm.get(pin)
            if v:                                    # VLM-processed -> VLM typing wins
                row["type_token"] = v["type_token"]; row["type_method"] = "vlm"
                row["os_style"] = v["os_style"]; row["legible"] = v["legible"]
                if v["vlm_text"]:
                    row["name"] = v["vlm_text"]
            else:                                    # Tier-0 typing
                ty = rec.get("type") or {}
                row["type_token"] = ty.get("token"); row["type_method"] = ty.get("method") or "tier0"
            # HCT county via label centre
            c = label_centre(rec)
            if c is not None:
                pt = Point(*c)
                for i in tree.query(pt):
                    if prep[i].contains(pt):
                        row["hc_county"] = codes[i]
                        d_m = geoms[i].boundary.distance(pt) * 111000.0
                        if d_m < a.uncertain_m:
                            row["hc_county_uncertain"] = True; row["hc_county_border_m"] = round(d_m)
                        break
            w.writerow(row)
            if n % 200000 == 0:
                print(f"[csv] {n:,} rows")
    print(f"[csv] wrote {n:,} rows -> {a.out}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--records", required=True)
    p.add_argument("--vlm-glob", required=True)
    p.add_argument("--hct", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--uncertain-m", type=float, default=100.0)
    run(p.parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
