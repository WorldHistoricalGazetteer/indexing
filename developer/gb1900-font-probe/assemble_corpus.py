"""Assemble the WHOLE series into labels — region by region, model loaded once.

Why not just point `assemble_labels.py` at all 35,514 files: its cost is violently superlinear in word
count. 122k words assemble in ~33 min at 27 G; 412k words did not finish in 8 h at 182 G. The series holds
16.77 M detections, so a joint run is not merely slow, it is impossible.

It is also unnecessary, and that is the point. Assembly is LOCAL. Spotting runs on a grid whose neighbours
overlap by ~512 px — deliberately more than a label is long — so **every label lies wholly inside at least
one region**. Assembling each region independently therefore loses nothing, turns an O(n^1.5+) problem into
a linear one, and parallelises across a Slurm array. Words duplicated in the overlap produce duplicate
LABELS, which are removed afterwards by the same identity the pipeline already uses for words.

The model is loaded ONCE per shard. join_rf7.joblib is 341 MB; loading it per region would dominate a job
whose per-region work is a couple of seconds.

    python assemble_corpus.py --shard 0 --of 32 --model join_rf7.joblib --out-dir .../labels
    python assemble_corpus.py --merge --out-dir .../labels --out .../gb_stamp_labels.jsonl
"""
import argparse, glob, json, os, sys, time

import numpy as np

from assemble_labels import assemble, norm, word_frame


def region_words(path, fonts=None):
    """One region's detections → the word records `assemble` expects."""
    words, seen = [], set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            p = r.get("gpoly")
            txt = str(r.get("text", "")).strip()
            if not p or not txt:
                continue
            cx = r.get("gcx") or sum(q[0] for q in p) / len(p)
            cy = r.get("gcy") or sum(q[1] for q in p) / len(p)
            k = (round(cx / 4), round(cy / 4), norm(txt))
            if k in seen:
                continue
            seen.add(k)
            wf = word_frame(p, txt, r.get("gline"))
            if wf is None:
                continue                                   # degenerate outline — see word_frame
            words.append(dict(id=len(words), text=txt, poly=p, f=wf,
                              font=(fonts or {}).get(k)))
    return words


def label_key(lab):
    """Identity of an assembled label: its member words' rounded centroids, in reading order.

    The SAME label assembled from two overlapping regions must collapse to one record. Keying on the
    ordered member positions (not the text) means a label that two regions read differently is still
    recognised as one label — and keeps the reading order that makes MOOR MIDDLETON distinct from
    MIDDLETON MOOR.
    """
    return tuple((round(m["f"]["cx"] / 4), round(m["f"]["cy"] / 4)) for m in lab["members"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boxes", default="/vast/ishi/gb1900/edition/spot2/boxes_gb_*.jsonl")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--of", type=int, default=1)
    ap.add_argument("--model", default="join_rf7.joblib")
    ap.add_argument("--model-thr", type=float, default=0.5)
    ap.add_argument("--max-lines", type=int, default=3)
    ap.add_argument("--centre-tol", type=float, default=0.25)
    ap.add_argument("--max-gap-pitch", type=float, default=2.5)
    ap.add_argument("--lat-tol", type=float, default=0.6)
    ap.add_argument("--h-tol", type=float, default=0.32)
    ap.add_argument("--ang-tol", type=float, default=12.0)
    ap.add_argument("--line-gap", type=float, default=2.4)
    ap.add_argument("--out-dir", default="/vast/ishi/gb1900/edition/labels")
    ap.add_argument("--merge", action="store_true", help="dedup the shard outputs into one file")
    ap.add_argument("--out", default="/vast/ishi/gb1900/edition/gb_stamp_labels.jsonl")
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)

    if a.merge:
        return merge(a)

    files = sorted(glob.glob(a.boxes))
    mine = [f for i, f in enumerate(files) if i % a.of == a.shard]
    print(f"shard {a.shard}/{a.of}: {len(mine)} of {len(files)} regions", flush=True)

    import joblib
    MODEL = joblib.load(a.model)
    print(f"model {a.model}: pair AUC {MODEL.get('auc'):.4f}, threshold {a.model_thr}", flush=True)

    outp = os.path.join(a.out_dir, f"labels_{a.shard:03d}.jsonl")
    t0 = time.time()
    n_lab = n_word = 0
    with open(outp, "w", encoding="utf-8") as out:
        for k, path in enumerate(mine):
            tag = os.path.basename(path)[6:-6]
            try:
                words = region_words(path)
                if not words:
                    continue
                labs = assemble(words, a.max_gap_pitch, a.lat_tol, a.h_tol, a.ang_tol, a.line_gap,
                                a.centre_tol, max_lines=a.max_lines, join_numerals=False,
                                model=MODEL, model_thr=a.model_thr)
            except Exception as e:                          # one bad region must not lose the shard
                print(f"FAIL {tag}: {type(e).__name__}: {e}", flush=True)
                continue
            n_word += len(words)
            for lab in labs:
                # `members` are INDICES into `words`, and the label already carries its own aggregate
                # geometry (text in reading order, cap height, angle, centroid, line count) — which is
                # precisely what typography_body() needs, so it is kept rather than recomputed.
                mem = [words[i] for i in lab["members"]]
                rec = dict(
                    region=tag,
                    text=lab["text"],
                    n_words=len(mem),
                    lines=lab["lines"],
                    h=round(lab["h"], 2),
                    ang=round(lab["ang"], 2),
                    gcx=round(lab["cx"], 1),
                    gcy=round(lab["cy"], 1),
                    key=[[round(m["f"]["cx"], 1), round(m["f"]["cy"], 1)] for m in mem],
                    words=[dict(text=m["text"],
                                cx=round(m["f"]["cx"], 1), cy=round(m["f"]["cy"], 1),
                                h=round(m["f"]["h"], 2), long=round(m["f"]["long"], 2),
                                ang=round(m["f"]["ang"], 2), poly=m["poly"]) for m in mem],
                )
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_lab += 1
            if (k + 1) % 500 == 0:
                print(f"  [{a.shard}] {k+1}/{len(mine)} regions, {n_lab} labels "
                      f"({time.time()-t0:.0f}s)", flush=True)
    print(f"SHARDLABELSDONE {a.shard}: {n_lab} labels from {n_word} words in {len(mine)} regions "
          f"({time.time()-t0:.0f}s)", flush=True)


def merge(a):
    """Collapse the shards, dropping the duplicates that region overlap necessarily produces."""
    seen = set()
    n_in = n_out = 0
    parts = sorted(glob.glob(os.path.join(a.out_dir, "labels_*.jsonl")))
    print(f"merging {len(parts)} shard files", flush=True)
    with open(a.out, "w", encoding="utf-8") as out:
        for p in parts:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    n_in += 1
                    rec = json.loads(line)
                    k = tuple((round(x / 4), round(y / 4)) for x, y in rec["key"])
                    if k in seen:
                        continue
                    seen.add(k)
                    out.write(line + "\n")
                    n_out += 1
    dup = n_in - n_out
    print(f"MERGEDONE {n_out:,} labels written, {dup:,} duplicates dropped "
          f"({dup/max(1,n_in):.1%} — expected, regions overlap by ~512px)", flush=True)


if __name__ == "__main__":
    main()
