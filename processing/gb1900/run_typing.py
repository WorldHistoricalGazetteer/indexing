"""Run GB-STAMP type_assign v2 over the FULL corpus -> top-3 (type, prob) per label, descending.
Output JSONL: {place_id, pin_id, text, types: [[type, prob], ...]}  (types[0] = best guess).
    python -m processing.gb1900.run_typing --nt national_typed.jsonl --names admin_names.json --out gb_stamp_types.jsonl
"""
import argparse, json, time, os, sys
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from type_assign import assign_types
except ImportError:
    from processing.gb1900.type_assign import assign_types

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nt", required=True); ap.add_argument("--names", default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    names = None
    if a.names:
        d = json.load(open(a.names)); names = set(d.get("names", []))
        print("settlement names:", len(names), flush=True)
    n = 0; top = Counter(); t0 = time.time()
    with open(a.out, "w") as w:
        for line in open(a.nt):
            try: d = json.loads(line)
            except Exception: continue
            tv = d.get("text"); tv = tv.get("value") if isinstance(tv, dict) else tv
            types = assign_types(tv, d.get("tier0_rule"), d.get("allcaps"), names)
            w.write(json.dumps({"place_id": d.get("place_id"), "pin_id": d.get("pin_id"),
                                "text": tv, "types": [[k, p] for k, p in types]}, ensure_ascii=False) + "\n")
            n += 1; top[types[0][0]] += 1
            if n % 500000 == 0: print(n, "(%.0fs)" % (time.time() - t0), flush=True)
    print("DONE", n, "labels (%.0fs)" % (time.time() - t0), flush=True)
    print("top-type distribution:", top.most_common(), flush=True)

if __name__ == "__main__":
    main()
