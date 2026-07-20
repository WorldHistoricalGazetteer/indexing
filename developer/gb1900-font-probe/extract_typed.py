"""Extract ALL typed GB-STAMP labels (type != null) to newline-delimited GeoJSON for tippecanoe, plus a
compact types.json sidecar (type -> count) that drives the map's AAT multi-select filter without the browser
having to scan the whole tileset. This is the full type layer — most of it (lexicon + the font-independent
OS single-letter rules like W=Well) is independent of font-spotting coverage, so the map is rich immediately.

    python extract_typed.py                 # -> gb_stamp_typed.geojsonl + types.json
"""
import json, os
from collections import Counter
IN = "/vast/ishi/gb1900/edition/gb_stamp.jsonl"
OUTL = "/vast/ishi/gb1900/edition/gb_stamp_typed.geojsonl"
OUTT = "/vast/ishi/gb1900/edition/types.json"

def main():
    cnt = Counter(); n = 0
    with open(OUTL, "w") as f:
        for line in open(IN):
            try: r = json.loads(line)
            except Exception: continue
            ty = r.get("type")
            if not ty or r.get("lon") is None: continue
            cnt[ty] += 1; n += 1
            feat = {"type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [round(r["lon"], 6), round(r["lat"], 6)]},
                    "properties": {"t": r.get("text"), "ty": ty, "aat": r.get("aat_term"),
                                   "fs": r.get("font_style"), "src": r.get("type_source")}}
            f.write(json.dumps(feat, ensure_ascii=False) + "\n")
    types = [{"key": k, "n": c} for k, c in cnt.most_common()]
    json.dump({"total": n, "types": types}, open(OUTT, "w"), ensure_ascii=False)
    print(f"wrote {n} typed features -> {OUTL} ({os.path.getsize(OUTL)//1024//1024} MB); "
          f"{len(types)} types -> {OUTT}")
    print("top types:", dict(cnt.most_common(12)))

if __name__ == "__main__":
    main()
