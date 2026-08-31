"""Backbone descriptors for the 225 human-labelled test boxes, via the SAME crop path as the harvest.

Both sides must go through `derotate` + white square-pad + 512² + ViTAEv2, or the comparison in
anchor_decisive_test.py measures the crop convention rather than the lettering.
"""
import json, sys
import numpy as np

sys.path.insert(0, "/vast/ishi/gb1900/probe/font")
from make_font_testset_v2 import derotate
from harvest_word_descriptors import backbone_concat, square512

SPOT = "/vast/ishi/gb1900/edition/spot"
boxes = json.load(open(f"{SPOT}/font_testset_v2_boxes.json"))
dec = json.load(open("/vast/ishi/gb1900/probe/font/font_testset_decisions_1.json"))
by_i = {d["i"]: d.get("font") for d in dec if isinstance(d, dict)}

D, Y, T = [], [], []
for i, b in enumerate(boxes):
    f = by_i.get(i)
    if not f:
        continue
    im = square512(derotate(b))
    if im is None:
        continue
    d = backbone_concat(im)
    if d is None:
        continue
    D.append(d.astype(np.float16)); Y.append(f); T.append(str(b.get("text", "")))
np.savez_compressed("/vast/ishi/gb1900/probe/font/labels/testset_desc.npz",
                    desc=np.array(D, np.float16), font=np.array(Y), text=np.array(T))
from collections import Counter
print(f"TESTDESCDONE {len(D)} descriptors  {dict(Counter(Y))}")
