"""Render the anchor crop columns to a page, so a crop bug can never again hide behind a plausible number.

The whole Phase-B comparison rests on three crops of the same anchor differing only in which polygon defined
them. That is exactly the kind of thing that looks fine in a table while being wrong in the image — as it was:
every crop was a map region, and the number still came out near its expected value.

    python crop_qc_ui.py --npz anchor_crops_hisam.npz --out anchor_crops_qc.html
"""
import argparse, base64, io, json, os
import numpy as np
from PIL import Image


def b64(a, maxh=90):
    im = Image.fromarray(np.asarray(a, np.uint8)).convert("L")
    if im.height > maxh:
        im = im.resize((max(1, int(im.width * maxh / im.height)), maxh), Image.LANCZOS)
    b = io.BytesIO()
    im.save(b, "PNG")
    return base64.b64encode(b.getvalue()).decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default="/vast/ishi/gb1900/edition/spot/anchor_crops_hisam.npz")
    ap.add_argument("--out", default="/vast/ishi/gb1900/edition/clean/anchor_crops_qc.html")
    ap.add_argument("--n", type=int, default=80)
    a = ap.parse_args()
    d = np.load(a.npz, allow_pickle=True)
    sigs = d["sigs"].astype(str)
    texts = d["texts"].astype(str) if "texts" in d.files else np.array([""] * len(sigs))
    items = []
    for i in range(min(a.n, len(sigs))):
        items.append(dict(sig=sigs[i], text=texts[i],
                          mr=b64(d["mr"][i]), word=b64(d["word"][i]), line=b64(d["line"][i])))
    html = HTML.replace("__DATA__", json.dumps(dict(items=items, n=len(sigs))))
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    open(a.out, "w").write(html)
    print(f"{len(sigs)} anchors; wrote {a.out} ({os.path.getsize(a.out)/1e6:.2f} MB)")
    print("CROPQCUIDONE")


HTML = r"""<!doctype html><meta charset=utf-8><title>GB-STAMP · anchor crop QC</title>
<style>
 body{font:13px system-ui;margin:0;background:#f4f2ee}
 header{position:sticky;top:0;background:#2a2622;color:#f4f2ee;padding:8px 14px}
 table{border-collapse:collapse;margin:10px} td,th{border:1px solid #ddd;padding:4px 6px;background:#fff;
   vertical-align:middle} th{background:#efece8;font-size:11px;position:sticky;top:34px}
 img{image-rendering:pixelated;background:#fff;max-width:420px}
 .t{font-size:11px;color:#555;max-width:150px;word-break:break-word}
</style>
<header><b>anchor crop QC</b> — <span id=s></span> · the three crop conventions, same anchor</header>
<table><thead><tr><th>text</th><th>signature</th><th>MapReader box</th><th>Hi-SAM word</th><th>Hi-SAM line</th></tr></thead>
<tbody id=b></tbody></table>
<script>
const D=__DATA__;
document.getElementById('s').textContent=`${D.items.length} of ${D.n} anchors`;
document.getElementById('b').innerHTML=D.items.map(i=>`<tr>
 <td class=t>${i.text}</td><td class=t>${i.sig}</td>
 <td><img src="data:image/png;base64,${i.mr}"></td>
 <td><img src="data:image/png;base64,${i.word}"></td>
 <td><img src="data:image/png;base64,${i.line}"></td></tr>`).join('');
</script>
"""

if __name__ == "__main__":
    main()
