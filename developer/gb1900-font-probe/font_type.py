"""Production font-style typing for the z17 campaign (GB-STAMP).

Loads the z17 CRNN + the sheet-grounded fusion classifier (font_ground.py -> font_clf.joblib) and
turns a batch of z17 label crops into HIGH-PRECISION additive votes that merge into the text typer's
(type, prob) distribution. Only styles that clear a precision bar in ground_report.json contribute;
everything else leaves the text distribution untouched (font is enrichment, never override).

STYLE_TYPE weights are the per-class precisions measured by font_ground.py (5-fold CV) — set/verified
from out_ground/ground_report.json so the vote mass = P(style|crop) x P(correct|style).
"""
import json, math, numpy as np, torch, joblib
import crnn_data as CD
from crnn import CRNN
from crnn_eval import crnn_embed
from fusion import textfeats

# style -> (type token, weight). Weights = measured reliability (font_ground.py 5-fold CV +
# clean hand-anchor eval): blackletter is genuinely ~0.9 (auto-label noise depresses its CV number);
# water-italic re-grounds to 0.72 (vs old settlement-italic 0.14 — usable SOFT signal); visual caps is
# detected at 0.96 but caps->TYPE is ambiguous (admin/town/road) so it gets a low sheet-justified vote.
# `upright` (settlement residual) and `numeral` are NOT asserted — the text typer already handles them.
STYLE_TYPE = {
    "blackletter": ("antiquity", 0.80),
    "italic": ("water_feature", 0.70),
    "caps": ("admin_or_parish", 0.55),
}
CONF_MIN = 0.60          # ignore low-confidence font predictions (applied to BASE-RATE-CORRECTED conf)
VOTE_CAP = 0.55          # max vote mass a single font signal can inject (keeps the text typer in control)

# BASE-RATE CORRECTION (critical): the classifier is fit on a BALANCED 5-class set (0.20 prior each),
# but real style frequencies are wildly skewed — blackletter(antiquity)~1.5%, caps(admin)~1.5%,
# italic(water)~7%, upright(everything else)~83%, numeral~4% (from the text-edition top-1 distribution).
# Without correction the balanced classifier over-fires the rare classes (smoke test: 20% "antiquity",
# 36% "water" on a Highland band — islands/lochs mislabelled). We reweight each posterior by
# prior_true/prior_train and renormalise, so a rare class only wins when the evidence is overwhelming.
PRIORS = {"upright": 0.82, "italic": 0.07, "caps": 0.05, "numeral": 0.04, "blackletter": 0.02}
TRAIN_PRIOR = 0.20


def _ink_h(im):
    rows = (im < 0.5).sum(axis=1); on = np.where(rows > 2)[0]
    return math.log(max(1.0, (on[-1] - on[0]) if len(on) else 1.0))


class FontTyper:
    def __init__(self, model_dir, clf_path, dev="cpu"):
        vocab = json.load(open(f"{model_dir}/vocab.json"))
        self.net = CRNN(n_class=len(vocab["stoi"]) + 1).to(dev)
        self.net.load_state_dict(torch.load(f"{model_dir}/crnn_z17.pt", map_location=dev))
        self.net.eval()
        d = joblib.load(clf_path)
        self.model = d["model"]; self.classes = [str(c) for c in self.model.classes_]; self.dev = dev
        # per-column base-rate correction factor (aligned to classifier's class order)
        self.factor = np.array([PRIORS.get(c, TRAIN_PRIOR) / TRAIN_PRIOR for c in self.classes])

    def classify(self, crops, texts):
        """crops: list of _to_h32 arrays; texts: parallel list. -> list of (style, corrected_conf).
        Posteriors are base-rate corrected before argmax so rare styles only win on strong evidence."""
        if not crops: return []
        Zc = crnn_embed(self.net, crops, self.dev)
        sz = np.array([[_ink_h(im)] for im in crops])
        tx = np.array([textfeats(t) for t in texts])
        F = np.hstack([Zc, sz, tx])
        proba = self.model.predict_proba(F) * self.factor          # base-rate correction
        proba = proba / proba.sum(1, keepdims=True)                 # renormalise rows
        idx = proba.argmax(1)
        return [(self.classes[i], float(proba[r, i])) for r, i in enumerate(idx)]


def merge(text_dist, style, conf):
    """Merge one font (style, conf) into a text (type, prob) distribution -> new top-3 list.
    Additive weighted vote; renormalised. Font never removes a text type, only re-weights."""
    if conf < CONF_MIN or style not in STYLE_TYPE:
        return text_dist
    tok, prec = STYLE_TYPE[style]
    w = min(VOTE_CAP, conf * prec)
    d = {k: v for k, v in text_dist}
    d[tok] = d.get(tok, 0.0) + w
    s = sum(d.values()) or 1.0
    items = sorted(((k, v / s) for k, v in d.items()), key=lambda x: -x[1])[:3]
    return [[k, round(p, 3)] for k, p in items]
