"""D1 — emit the assembled labels as W3C Web Annotations carrying what the lettering physically is.

Each label becomes one annotation whose target is an `oa:List` of its member words in reading order, and
whose bodies are the transcription plus a `typography_body`. Word-level annotations are emitted too, so the
list members are dereferenceable rather than dangling.

WHAT IS MEASURED HERE, AND WHAT IS NOT.

  cap_height_px   the spotter polygon's SHORT side, following `build_alphabet_multi.gpoly_cap_h` — measured
                  from GEOMETRY, not pixels, because a derotated crop is full of dark cartographic detail
                  (contours, roads, buildings) and Otsu ink-extent just returns the crop size. It is a
                  proxy: for mixed-case words with ascenders and descenders it exceeds true cap height. The
                  convention is the project's own and is applied consistently.
  cap_height_m    the same height on the ground. Pixels are comparable only within one zoom level, so this
                  is the figure any later analysis actually wants. Web Mercator, so ground resolution is
                  latitude-dependent: 156543.034 * cos(lat) / 2^17 at z17. A Highland label and a Cornish
                  one of identical pixel height differ by ~9% on the ground.
  lines           multi-line labels are set as such deliberately.

  slant_deg       NOT emitted here. The assembled label's `ang` is the BASELINE ORIENTATION — how the label
                  is rotated on the map — and is not the italic slant of the letterforms. Recording one as
                  the other would corrupt precisely the signal the 1897 Characteristic Sheet assigns to
                  water and descriptive features. True slant needs the pixels (`slant_word.shear_slant`),
                  so it belongs to the crop pass with the face.
  face            NOT emitted here. Requires the classifier over real crops.

Height is deliberately kept OUT of the face. On the six-inch the typeface encodes feature TYPE and the size
encodes IMPORTANCE, per the Characteristic Sheet, so a parish name and a county name can share a face and be
separable by height alone. Collapsing them would discard half the signal.

    python emit_annotations.py --labels gb_stamp_labels.jsonl --out gb_stamp_annotations.jsonl
"""
import argparse, json, math, os, sys, time

from gbstamp_records import (Writer, annotation, canvas_target, ordered_target, software,
                             svg_selector, textual_body, typography_body)

N17 = 2 ** 17
BASE = "https://whgazetteer.org/gbstamp"
GENERATOR = software(f"{BASE}/software/gb-stamp", "GB-STAMP word spotter + learned label assembly")


def lat_of(gy):
    """Web Mercator inverse for a global z17 pixel row."""
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * gy / (N17 * 256)))))


def m_per_px(lat):
    return 156543.03392 * math.cos(math.radians(lat)) / (N17 * 256) * 256


def canvas_uri(region):
    """The IIIF canvas a detection sits on. One canvas per spotted region keeps the target addressable
    without inventing a per-label image."""
    return f"{BASE}/canvas/{region}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="/vast/ishi/gb1900/edition/gb_stamp_labels.jsonl")
    ap.add_argument("--out", default="/vast/ishi/gb1900/edition/gb_stamp_annotations.jsonl")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--of", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0, help="stop after N labels (smoke test)")
    a = ap.parse_args()

    out = a.out if a.of == 1 else a.out.replace(".jsonl", f".{a.shard:03d}.jsonl")
    w = Writer(out)
    t0 = time.time()
    n_lab = n_word = skipped = 0
    with open(a.labels, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i % a.of != a.shard:
                continue
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            words = rec.get("words") or []
            if not words:
                skipped += 1
                continue
            region = rec["region"]
            cu = canvas_uri(region)
            lat = lat_of(rec["gcy"])
            mpp = m_per_px(lat)

            member_ids = []
            for k, wd in enumerate(words):
                wid = f"{BASE}/word/{region}/{int(wd['cx'])}_{int(wd['cy'])}"
                w.write(annotation(
                    wid,
                    [textual_body(wd["text"], purpose="transcribing"),
                     typography_body(cap_height_px=wd["h"], cap_height_m=wd["h"] * mpp)],
                    canvas_target(cu, svg_selector(wd["poly"])),
                    motivation="transcribing", generator=GENERATOR))
                member_ids.append(wid)
                n_word += 1

            lid = f"{BASE}/label/{region}/{int(rec['gcx'])}_{int(rec['gcy'])}"
            w.write(annotation(
                lid,
                [textual_body(rec["text"], purpose="transcribing"),
                 typography_body(cap_height_px=rec["h"], cap_height_m=rec["h"] * mpp,
                                 lines=rec.get("lines", 1))],
                ordered_target(member_ids),
                motivation="transcribing", generator=GENERATOR))
            n_lab += 1
            if a.limit and n_lab >= a.limit:
                break
            if n_lab % 200000 == 0:
                print(f"  {n_lab:,} labels, {n_word:,} words ({time.time()-t0:.0f}s)", flush=True)
    w.close()
    print(f"ANNOTATEDONE shard {a.shard}: {n_lab:,} label annotations + {n_word:,} word annotations "
          f"-> {out} ({skipped} skipped, {time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
