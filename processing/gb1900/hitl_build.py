#!/usr/bin/env python
"""Inject a HITL manifest.json into the review-tool HTML template -> self-contained artifact.
  python -m processing.gb1900.hitl_build manifest.json [template.html] [out.html]
The `</`->`<\\/` escape keeps label text from breaking the JSON <script> block (\\/ == /)."""
import sys
from pathlib import Path
def build(manifest, template, out):
    html = Path(template).read_text(encoding="utf-8")
    man = Path(manifest).read_text(encoding="utf-8").replace("</", "<\\/")
    Path(out).write_text(html.replace("__MANIFEST__", man), encoding="utf-8")
    print(f"wrote {out} ({Path(out).stat().st_size/1e6:.1f} MB)")
if __name__ == "__main__":
    a = sys.argv
    build(a[1], a[2] if len(a) > 2 else str(Path(__file__).parent/"font_hitl_review.html"),
          a[3] if len(a) > 3 else "hitl_review_final.html")
