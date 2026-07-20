"""Extract GB-STAMP labels to newline-delimited GeoJSON for tippecanoe, plus a compact types.json sidecar
(type -> count) that drives the map's AAT multi-select filter without the browser scanning the whole tileset.

Default emits the typed layer (type != null); --all also emits the ~1.79M untyped crowd labels as
ty="untyped" so the FULL GB1900 crowd set is visible on the map (grey, still filterable/toggleable). Most
typing is lexicon + font-independent OS single-letter rules (W=Well), so it is rich independent of spotting.

    python extract_typed.py                 # typed only
    python extract_typed.py --all           # every crowd label (untyped -> ty="untyped")
"""
import argparse, json, os
from collections import Counter
IN = "/vast/ishi/gb1900/edition/gb_stamp.jsonl"
OUTL = "/vast/ishi/gb1900/edition/gb_stamp_typed.geojsonl"
OUTT = "/vast/ishi/gb1900/edition/types.json"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="also emit untyped crowd labels (ty=untyped)")
    a = ap.parse_args()
    cnt = Counter(); n = 0; untyped = 0
    with open(OUTL, "w") as f:
        for line in open(IN):
            try: r = json.loads(line)
            except Exception: continue
            if r.get("lon") is None: continue
            ty = r.get("type")
            if not ty:
                if not a.all: continue
                ty = "untyped"; untyped += 1
                props = {"t": r.get("text"), "ty": ty}
            else:
                props = {"t": r.get("text"), "ty": ty, "aat": r.get("aat_term"),
                         "fs": r.get("font_style"), "src": r.get("type_source")}
                ft = r.get("font_top3")           # ranked shortlist -> "font conf|font conf|…" for the map pie
                if ft: props["ft"] = "|".join(f"{f} {c}" for f, c in ft)
            cnt[ty] += 1; n += 1
            f.write(json.dumps({"type": "Feature",
                                "geometry": {"type": "Point", "coordinates": [round(r["lon"], 6), round(r["lat"], 6)]},
                                "properties": props}, ensure_ascii=False) + "\n")
    # sidecar: typed classes by frequency, untyped always last
    types = [{"key": k, "n": c} for k, c in cnt.most_common() if k != "untyped"]
    if "untyped" in cnt: types.append({"key": "untyped", "n": cnt["untyped"]})
    json.dump({"total": n, "typed": n - untyped, "types": types}, open(OUTT, "w"), ensure_ascii=False)
    print(f"wrote {n} features ({untyped} untyped) -> {OUTL} ({os.path.getsize(OUTL)//1024//1024} MB); "
          f"{len(types)} types -> {OUTT}")
    print("top types:", dict(cnt.most_common(12)))

if __name__ == "__main__":
    main()
