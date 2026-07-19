"""Freeze the v2 test-set box records so validation is independent of the growing spotter pool.

make_font_testset_v2 re-derives the sample via stratified(load()) over boxes_*.jsonl; once the spotter batch
adds sheets, that sample changes and no longer matches the labelled boxes. This reproduces the ORIGINAL
sample from ONLY the 4 pilot sheets (the pool at v2-generation time), verifies it matches the decisions by
text, and writes font_testset_v2_boxes.json (the exact box records in order) for all downstream validators.

    /vast/ishi/envs/boundary/bin/python freeze_testset.py
"""
import sys; sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
import glob, json
from make_font_testset_v2 import SPOT, stratified

PILOT = {"amesbury", "aberystwyth", "york", "dorchester"}
DEC = "/vast/ishi/gb1900/probe/font/font_testset_decisions_1.json"
OUT = f"{SPOT}/font_testset_v2_boxes.json"

def load_pilot():
    out = []
    for f in glob.glob(f"{SPOT}/boxes_*.jsonl"):
        tag = f.split("boxes_")[1][:-6]
        if tag not in PILOT: continue
        for line in open(f):
            r = json.loads(line)
            if len([c for c in r["text"] if c.isalnum()]) >= 3 and r["score"] >= 0.55: out.append(r)
    return out

def main():
    samp = stratified(load_pilot())
    dec = json.load(open(DEC))
    m = sum(1 for i in range(min(len(samp), len(dec))) if samp[i]["text"] == dec[i]["text"])
    print(f"sample={len(samp)} decisions={len(dec)}  text-match={m}/{len(dec)}")
    if m < 0.9 * len(dec):
        print("WARNING: low match — glob order differs; sample not faithfully reproduced.")
    json.dump(samp, open(OUT, "w"))
    print(f"wrote {OUT}")

if __name__ == "__main__":
    main()
