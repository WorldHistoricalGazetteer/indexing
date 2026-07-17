#!/usr/bin/env python
"""VLM boundary-tracing probe — can a VLM localise the mereing boundary by grid cell?

For each gridded crop, ask the VLM which grid cells the administrative (mereing) boundary
passes through, disambiguating it from field lines / hedges / footpaths / river banks / text
— the reasoning task that defeats the pixel CV. Score vs GT cells (positives) + false-alarm
rate (negatives). Bounded honesty check for the boundary-extraction R&D.
"""
import argparse, base64, json, sys
from pathlib import Path
import httpx

SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["boundary_present", "cells"],
          "properties": {"boundary_present": {"type": "boolean"},
                         "cells": {"type": "array", "items": {"type": "string"}}}}

PROMPT = (
    "This is a 512px crop of an Ordnance Survey six-inch map with an orange 6x6 GRID "
    "overlaid; each cell is labelled 'RC' where R is the row (0=top..5=bottom) and C is the "
    "column (0=left..5=right).\n"
    "An ADMINISTRATIVE BOUNDARY (parish / district / union / county) is drawn in the OS "
    "'mereing' style: a line of round DOTS, punctuated by occasional bold x cross-marks "
    "(set a little to ONE SIDE of the line) and small arrows. You must DISTINGUISH it from "
    "look-alikes that are NOT the boundary:\n"
    "- thin SOLID lines = field/enclosure edges;\n"
    "- single DASHED lines = hedges/tracks; DOUBLE parallel dashed = footpaths;\n"
    "- rows of short TICKS / hachures along a double line = river or stream BANKS;\n"
    "- text, contour lines, buildings.\n"
    "Find the dotted mereing boundary IF one is present, and list — in order along the line "
    "— the grid cells (as 'RC' codes) that its line passes through. If there is NO dotted "
    "mereing boundary in the crop, return boundary_present=false and an empty list. "
    "Return JSON per the schema."
)


def query(client, endpoint, model, path):
    b64 = base64.b64encode(Path(path).read_bytes()).decode()
    body = {"model": model, "temperature": 0, "max_tokens": 300,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "trace", "strict": True, "schema": SCHEMA}}}
    r = client.post(f"{endpoint}/chat/completions", json=body, timeout=120)
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


def neighbours(cell):
    r, c = int(cell[0]), int(cell[1])
    return {f"{r+dr}{c+dc}" for dr in (-1, 0, 1) for dc in (-1, 0, 1)
            if 0 <= r+dr <= 5 and 0 <= c+dc <= 5}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta", required=True); ap.add_argument("--dir", required=True)
    ap.add_argument("--endpoint", required=True); ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    meta = json.load(open(a.meta))
    rows = []
    pos_rec = []; pos_prec = []; neg_fp = 0; neg_n = 0
    with httpx.Client() as client:
        for m in meta["crops"]:
            try:
                v = query(client, a.endpoint, a.model, str(Path(a.dir) / m["file"]))
            except Exception as e:
                v = {"_error": str(e)[:100]}
            pred = set(v.get("cells", []) or [])
            gt = set(m["gt_cells"])
            # adjacency-tolerant match (cell resolution ~85px; boundary is thin)
            gt_adj = set().union(*[neighbours(c) for c in gt]) if gt else set()
            hit = len(pred & gt_adj)
            row = {"file": m["file"], "tag": m["tag"], "pred": sorted(pred),
                   "gt": sorted(gt), "present": v.get("boundary_present"), "err": v.get("_error")}
            rows.append(row)
            if m["tag"] == "pos":
                pos_rec.append(len(pred & gt_adj) / max(len(gt), 1))
                pos_prec.append(hit / max(len(pred), 1))
            else:
                neg_n += 1
                if v.get("boundary_present") or pred: neg_fp += 1
            print(f"[{m['tag']}] {m['file']}: present={v.get('boundary_present')} "
                  f"pred={sorted(pred)} gt={sorted(gt)}")
    summ = {"pos_recall": round(sum(pos_rec)/max(len(pos_rec), 1), 3),
            "pos_precision": round(sum(pos_prec)/max(len(pos_prec), 1), 3),
            "neg_false_alarm": f"{neg_fp}/{neg_n}"}
    json.dump({"summary": summ, "rows": rows}, open(a.out, "w"), indent=1)
    print("[vlm-trace] SUMMARY", json.dumps(summ))


if __name__ == "__main__":
    sys.exit(main())
