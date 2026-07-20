"""Extract the FONT-TYPED GB-STAMP labels (the actual new-method output) to GeoJSON for the MapLibre demo.
Only labels with a confident font style are included — these are what GB-STAMP contributes. Small enough
(tens of thousands) for MapLibre to render directly from GeoJSON, no tiler needed."""
import json
IN = "/vast/ishi/gb1900/edition/gb_stamp.jsonl"
OUT = "/vast/ishi/gb1900/edition/gb_stamp_font.geojson"
from collections import Counter
feats = []; fd = Counter()
for line in open(IN):
    try: r = json.loads(line)
    except Exception: continue
    if not r.get("font_style") or r.get("lon") is None: continue
    fd[r["font_style"]] += 1
    feats.append({"type": "Feature",
                  "geometry": {"type": "Point", "coordinates": [round(r["lon"], 6), round(r["lat"], 6)]},
                  "properties": {"t": r["text"], "ty": r.get("type"), "fs": r["font_style"],
                                 "fc": r.get("font_conf"), "aat": r.get("aat_term")}})
json.dump({"type": "FeatureCollection", "features": feats}, open(OUT, "w"), ensure_ascii=False)
print(f"wrote {len(feats)} font-typed features -> {OUT}; font dist {dict(fd)}; "
      f"size {__import__('os').path.getsize(OUT)//1024} KB")
