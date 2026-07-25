"""Does the crop actually contain TEXT? Measured with a text detector, not with ink statistics.

A first attempt used Otsu ink fraction/contrast and found nothing (0% "blank" corpus-wide) — because building
hatching, road casings and paper texture have the same ink statistics as lettering. That measure cannot answer
the question, so this uses the right instrument: Hi-SAM's own pixel text-foreground head, run on the crop.
Whatever it calls foreground IS text, by construction of its training.

Why it matters: re-presenting the cards a labeller could not label showed masks sitting on blank ground and on
building hatching, with the transcript's label nowhere in the crop. Those descriptors carry no font information
but are indexed under a real GB1900 transcript, so they are worse than missing — they are mislabelled data.

Reports the corpus rate and the labelled cards separately, so the human's "unclear" verdicts serve as an
independent check on the measure: if it is any good, the 9 unclear should score far worse than the 231 labelled.

    python crop_qc_text.py --sample 3000 --labels "pool_labels_round (4).json"   # hisam env, GPU
"""
import argparse, glob, json, os, random, sys, numpy as np, cv2

sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
REPO = "/vast/ishi/gb1900/probe/hisam/Hi-SAM"
sys.path.insert(0, REPO)
import torch
from types import SimpleNamespace
from hi_sam.modeling.build import model_registry
from hi_sam.modeling.auto_mask_generator import AutoMaskGenerator
from make_font_testset_v2 import derotate

PINS = "/vast/ishi/gb1900/edition/pins"


def key(gx, gy):
    return (round(float(gx), 1), round(float(gy), 1))


def build(mt, ckpt):
    args = SimpleNamespace(model_type=mt, checkpoint=os.path.abspath(ckpt), hier_det=True,
                           attn_layers=1, prompt_len=12, input_size=[1024, 1024], device="cuda")
    cwd = os.getcwd()
    os.chdir(REPO)                              # build reads pretrained_checkpoint/ relative to CWD
    try:
        m = model_registry[mt](args)
    finally:
        os.chdir(cwd)
    m.to("cuda")
    m.eval()
    return m, AutoMaskGenerator(m)


@torch.no_grad()
def text_fraction(amg, model, crop_gray):
    """Fraction of the crop's area that Hi-SAM calls text."""
    amg.set_image(np.repeat(crop_gray[:, :, None], 3, 2))
    feat = amg.features
    sparse = model.modal_aligner(feat)
    _, high, _, _ = model.mask_decoder(image_embeddings=feat, image_pe=model.prompt_encoder.get_dense_pe(),
                                       sparse_prompt_embeddings=sparse, multimask_output=False)
    fg = (high > model.mask_threshold).squeeze(1)[0].cpu().numpy().astype(np.uint8)
    h, w = crop_gray.shape[:2]
    s = 1024.0 / max(h, w)
    rh, rw = max(1, int(h * s)), max(1, int(w * s))
    return float(fg[:rh, :rw].mean())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pins-dir", default=PINS)
    ap.add_argument("--labels", default=None)
    ap.add_argument("--sample", type=int, default=3000)
    ap.add_argument("--weight", default="/vast/ishi/gb1900/probe/hisam/weights/hi_sam_l.pth")
    ap.add_argument("--model-type", default="vit_l")
    ap.add_argument("--no-text", type=float, default=0.02, help="text fraction below which the crop has no label")
    ap.add_argument("--out", default=f"{PINS}/crop_qc_text.json")
    a = ap.parse_args()
    random.seed(42)

    verified = {}
    if a.labels and os.path.exists(a.labels):
        verified = {key(x["gcx"], x["gcy"]): x.get("sig") for x in json.load(open(a.labels))}

    recs = []
    for f in sorted(glob.glob(f"{a.pins_dir}/pins_*.jsonl")):
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("line_gpoly") or r.get("gpoly"):
                recs.append(r)
    labelled = [r for r in recs if key(r["gcx"], r["gcy"]) in verified]
    pool = random.sample(recs, min(a.sample, len(recs)))
    print(f"{len(recs)} detections; measuring {len(pool)} sampled + {len(labelled)} labelled", flush=True)

    model, amg = build(a.model_type, a.weight)

    def run(rows, name):
        out = []
        for i, r in enumerate(rows):
            crop = derotate({"gpoly": r.get("line_gpoly") or r.get("gpoly")})
            if crop is None or crop.size < 80:
                continue
            try:
                tf = text_fraction(amg, model, crop)
            except torch.cuda.OutOfMemoryError:
                torch.cuda.empty_cache()
                continue
            out.append(dict(tf=tf, text=r.get("text", ""), gcx=r["gcx"], gcy=r["gcy"]))
            if i and i % 500 == 0:
                print(f"  [{name}] {i}/{len(rows)}", flush=True)
        if out:
            t = np.array([x["tf"] for x in out])
            bad = int((t < a.no_text).sum())
            print(f"\n{name}: n={len(out)}  text-fraction median {np.median(t):.3f}  "
                  f"p10 {np.percentile(t,10):.3f}", flush=True)
            print(f"  NO TEXT (< {a.no_text}): {bad} = {bad/len(out):.2%}", flush=True)
        return out

    corpus = run(pool, "corpus")
    lab = run(labelled, "labelled")

    res = dict(sampled=len(corpus), no_text_threshold=a.no_text,
               corpus_no_text_rate=float(np.mean([x["tf"] < a.no_text for x in corpus])) if corpus else None)
    if lab and verified:
        unclear = [x for x in lab if not verified.get(key(x["gcx"], x["gcy"]))]
        clear = [x for x in lab if verified.get(key(x["gcx"], x["gcy"]))]
        if unclear and clear:
            tu = np.array([x["tf"] for x in unclear])
            tc = np.array([x["tf"] for x in clear])
            print(f"\n  UNCLEAR cards: text-fraction median {np.median(tu):.3f} "
                  f"({int((tu < a.no_text).sum())}/{len(tu)} below threshold)", flush=True)
            print(f"  LABELLED cards: text-fraction median {np.median(tc):.3f} "
                  f"({int((tc < a.no_text).sum())}/{len(tc)} below threshold)", flush=True)
            res.update(unclear_median=float(np.median(tu)), clear_median=float(np.median(tc)))
        print("\n  least-texty crops among the labelled:", flush=True)
        for x in sorted(lab, key=lambda z: z["tf"])[:8]:
            mark = "UNCLEAR" if not verified.get(key(x["gcx"], x["gcy"])) else "labelled"
            print(f"    tf {x['tf']:.3f}  [{mark}]  {x['text'][:40]!r}", flush=True)

    json.dump(res, open(a.out, "w"), indent=2)
    print(f"\nwrote {a.out}\nQCTEXTDONE", flush=True)


if __name__ == "__main__":
    main()
