"""Phase B stage 1 — re-crop the 189 sig-anchors under the Hi-SAM box convention.

The 0.63 maxsim-LOO was measured on MapReader-box crops. Since Hi-SAM is now the single box authority
(NEXT-PHASE.md §1.2), that number has to be re-derived on Hi-SAM masks or it is not the pipeline's number.

The comparison is controlled: all three crops go through the SAME `make_font_testset_v2.derotate` geometry
(minAreaRect -> rotate to horizontal -> getRectSubPix with an 8 px pad), so the only thing that varies is which
polygon it is handed. Any difference in stage 2 is then attributable to the BOX, not to a crop reimplementation.

  mr    MapReader's word box .............. the control; should reproduce ~0.63
  word  Hi-SAM word mask at the prompt ..... one word of the label
  line  Hi-SAM line mask at the prompt ..... the whole label — the production crop unit

Prompts are placed at the anchor's own centroid rather than at a GB1900 pin, so all 189 anchors survive with
their existing `sig` labels; the crop convention under test is "line mask from a point prompt", which is
exactly what production does.

    python anchor_recrop.py          # hisam env, GPU, ~2 min
"""
import argparse, glob, json, os, sys, numpy as np, cv2

sys.path.insert(0, "/vast/ishi/gb1900/probe/font")               # make_font_testset_v2.derotate
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hisam_pins import build_model, prompt_pins, mask_poly, window_image, LINE, WORD
from make_font_testset_v2 import derotate

HERE = "/vast/ishi/gb1900/probe/font"
SPOT = "/vast/ishi/gb1900/edition/spot"


def key(gx, gy):
    return (round(float(gx), 1), round(float(gy), 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weight", default="/vast/ishi/gb1900/probe/hisam/weights/hi_sam_l.pth")
    ap.add_argument("--model-type", default="vit_l")
    ap.add_argument("--labels", default=f"{HERE}/labels/pool_labels_faced.json")
    ap.add_argument("--win", type=int, default=4, help="window side in tiles (4 = 1024px = Hi-SAM native)")
    ap.add_argument("--out", default=f"{SPOT}/anchor_crops_hisam.npz")
    ap.add_argument("--hisam", action="store_true",
                    help="also cut Hi-SAM word/line crops. Off by default: the comparison has been made and "
                         "MapReader boxes won (0.739 vs 0.660 line, 0.569 word), because Hi-SAM masks clip "
                         "words — Wood becomes ood. Keeping the columns costs a GPU and a second conda env "
                         "for crops nothing downstream uses.")
    a = ap.parse_args()

    # `face` (inventory vocabulary) wins over `sig` (legacy), so the anchor set migrates without a flag day.
    lab = [l for l in json.load(open(a.labels)) if l.get("face") or l.get("sig")]
    want = {key(l["gcx"], l["gcy"]) for l in lab}
    rec = {}
    for f in glob.glob(f"{SPOT}/boxes_*.jsonl"):
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:
                continue
            k = key(r["gcx"], r["gcy"])
            if k in want and k not in rec:
                rec[k] = r
        if len(rec) >= len(want):
            break
    print(f"{len(lab)} anchors, {len(rec)} matched to MapReader boxes", flush=True)

    model = amg = None
    if a.hisam:
        model, amg = build_model(a.model_type, a.weight)
    mr, hw, hl, sigs, texts, notes, gcxs, gcys = [], [], [], [], [], [], [], []
    nomask = nocrop = 0
    for l in lab:
        r = rec.get(key(l["gcx"], l["gcy"]))
        if r is None:
            nocrop += 1
            continue
        control = derotate(r)
        if control is None or control.size < 80:
            nocrop += 1
            continue
        gcx, gcy = float(r["gcx"]), float(r["gcy"])
        if not a.hisam:
            mr.append(control.astype(np.uint8))
            hw.append(control.astype(np.uint8))
            hl.append(control.astype(np.uint8))
            sigs.append(l.get("face") or l["sig"])
            texts.append(str(r.get("text", "")))
            gcxs.append(gcx)
            gcys.append(gcy)
            notes.append(dict(word_ok=False, line_ok=False))
            continue
        tx0 = int(gcx) // 256 - a.win // 2
        ty0 = int(gcy) // 256 - a.win // 2
        img, hit = window_image(tx0, ty0, a.win)
        if hit == 0:
            nocrop += 1
            continue
        ox, oy = tx0 * 256, ty0 * 256
        amg.set_image(img)
        words, hier, _ = prompt_pins(amg, model, np.array([[gcx - ox, gcy - oy]]))
        hs = (a.win * 256) / hier.shape[-1]
        wpoly, _ = mask_poly(words[0], 1.0, ox, oy)
        lpoly, _ = mask_poly(hier[0][LINE], hs, ox, oy)
        if wpoly is None and lpoly is None:
            nomask += 1
            continue
        cw = derotate({"gpoly": wpoly}) if wpoly else None
        cl = derotate({"gpoly": lpoly}) if lpoly else None
        if cw is None or cw.size < 80:
            cw = control                                          # fall back so the columns stay row-aligned;
        if cl is None or cl.size < 80:                            # `notes` records which rows were substituted
            cl = cw
        mr.append(control.astype(np.uint8))
        hw.append(cw.astype(np.uint8))
        hl.append(cl.astype(np.uint8))
        sigs.append(l.get("face") or l["sig"])
        texts.append(str(r.get("text", "")))
        gcxs.append(float(r["gcx"]))
        gcys.append(float(r["gcy"]))
        notes.append(dict(word_ok=bool(wpoly), line_ok=bool(lpoly)))

    # gcx/gcy travel with the crops so a rejection or correction made in the QC page can be keyed straight
    # back to the anchor in pool_labels.json, rather than relying on row order surviving a re-run.
    np.savez(a.out, mr=np.array(mr, object), word=np.array(hw, object), line=np.array(hl, object),
             sigs=np.array(sigs, object), texts=np.array(texts, object), notes=np.array(notes, object),
             gcx=np.array(gcxs), gcy=np.array(gcys))
    nw = sum(n["word_ok"] for n in notes)
    nl = sum(n["line_ok"] for n in notes)
    print(f"saved {len(mr)} anchors (no MapReader crop {nocrop}, no Hi-SAM mask {nomask}); "
          f"word mask {nw}, line mask {nl} -> {a.out}", flush=True)
    print("RECROPDONE", flush=True)


if __name__ == "__main__":
    main()
