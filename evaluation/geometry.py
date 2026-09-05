"""Gate 1 — the geometry of the embedding space, before anything is retrieved.

WHY A GEOMETRY GATE AT ALL. Symphonym v7 shipped, and only afterwards was it
measured to use **effective rank 10.83 of its 128 dimensions**: 109 components
carry essentially nothing, and every toponym in the corpus is squeezed into an
11-dimensional cone. That is visible in seconds from the singular-value spectrum
of a few thousand vectors and needs no labels, no queries and no ground truth —
which is exactly why it belongs before the expensive tests rather than after
them. The pairs test that *was* in place could not see it, because it only ever
asked whether known matches score high (they do, in a collapsed space
everything scores high) and never what non-matches score.

WHAT EACH MEASURE CATCHES — the column that matters, because a check whose
failure mode is unstated is a check nobody has shown can fail:

    effective_rank      dimensional collapse: the space is lower-dimensional
                        than its width, so distinct names cannot be kept apart.
                        Catches v7 (10.83/128).
    mean_norm           an off-centre cloud: every vector shares a large common
                        component, so cosines are dominated by the mean and not
                        by the name. Catches "everything scores 0.93".
    sigma20_over_1      the cliff: a spectrum that falls off a shelf rather
                        than decaying. v7 falls off at component 20.
    p50_cosine          the working range actually available to a threshold.
                        A median pairwise cosine near 1 means no threshold can
                        separate anything, whatever the pairs test reports.
    nn_saturation       the 200th-nearest neighbour is as close as the 1st:
                        a KNN of k=200 then returns 200 equally-good answers and
                        ranking within it is noise. This is the retrieval
                        failure the ranking benchmark measures expensively; the
                        spectrum predicts it for free.

The thresholds are DEFAULTS, and the point of them is that they are refutable:
each is stated with the v7 measurement it would have caught, so disagreeing with
a threshold means arguing about a number rather than about a feeling.

⚠ This gate says nothing about whether the space is *phonetically* right. A
random projection passes every one of these. It is a necessary condition, not a
sufficient one — which is why it is gate 1 of 3 and not the benchmark.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field

import numpy as np

#: Defaults, each annotated with what it would have caught. Passing all of them
#: is not evidence of quality; failing one is evidence of a defect.
#:
#: Thresholds whose calibration is not yet established, named here so the report
#: can mark them rather than presenting every threshold as equally load-bearing.
PROVISIONAL_THRESHOLDS = frozenset({"mean_norm_max"})

DEFAULT_THRESHOLDS = {
    # v7 measures 10.83. 40 of 128 is a third of the width — deliberately far
    # below "healthy" and still far above what shipped, so the gate fires on the
    # known defect without asserting a standard nothing has met.
    "effective_rank_min": 40.0,
    # ⚠ PROVISIONAL, and the output says so. v7 measures 0.2722 on 6,000 real
    # toponyms — it PASSES this threshold by 0.028. A threshold that
    # nearly-but-doesn't fire on the known-bad model is not calibrated; it is a
    # coin toss wearing a number. Kept because the quantity is meaningful (a
    # unit-norm space with no shared component has ||mean|| ~ 1/sqrt(N)), but
    # do not treat a pass here as evidence of anything until it has been
    # measured on a model that is known to be off-centre.
    "mean_norm_max": 0.30,
    # A spectrum that has lost 99% of its scale by component 20 has a cliff.
    "sigma20_over_1_min": 0.01,
    # If half of all random pairs already sit above this, no threshold works.
    "p50_cosine_max": 0.80,
    # The 200th neighbour must be measurably worse than the 1st, or a k=200 KNN
    # is returning ties.
    #
    # ⚠ CORPUS-SIZE DEPENDENT, AND A SMALL CORPUS UNDERSTATES THE DEFECT.
    # Saturation is a density effect, so the same model measured over more
    # vectors looks worse — correctly. Symphonym v7, one 1.05M-name corpus
    # sub-sampled, measured 5 Sep 2026:
    #
    #     n            1st nbr   200th nbr    gap
    #     6,000         0.8772     0.5666    0.3106   passes comfortably
    #     40,000        0.9187     0.7206    0.1980
    #     200,000       0.9426     0.8036    0.1390
    #     1,053,229     0.9623     0.8627    0.0995   twice the threshold
    #
    # The gap roughly halves per 30x of corpus and is heading for this
    # threshold. The live `toponyms` index is 72.7M — 69x this corpus — and
    # `gateway/es_helpers.knn_pass_quality` measured ">0.93 for the 200 nearest
    # neighbours of anything" against it on 2026-08-20, which is what this curve
    # extrapolates to and is irreconcilable with the 6,000-name figure alone.
    # So a PASS here is evidence only at the n it was measured at. Report n
    # beside it, and never compare gaps across corpus sizes.
    "nn_gap_min": 0.05,
}


@dataclass
class GeometryReport:
    n_vectors: int
    dim: int
    effective_rank: float
    mean_norm: float
    sigma20_over_1: float
    var_top1: float
    var_top10: float
    var_top20: float
    cosine_mean: float
    cosine_sd: float
    cosine_p50: float
    cosine_p99: float
    nn1_cosine: float
    nn200_cosine: float
    nn_gap: float
    thresholds: dict
    #: "exact" or "sampled(...)". Printed, because a neighbour cosine over
    #: 5,000 probes against 1M is a different measurement from one over every
    #: pair of 6,000, and the two must never be compared as if they were not.
    method: str = "exact"
    n_probe_rows: int = 0
    failures: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(asdict(self) | {"passed": self.passed}, indent=indent)

    def summary(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        lines = [
            f"[geometry] {verdict} — {self.n_vectors:,} vectors of dim {self.dim}"
            f"  [{self.method}"
            + (f", {self.n_probe_rows:,} probe rows]" if self.method != "exact" else "]"),
            f"  effective rank   {self.effective_rank:8.2f} of {self.dim}"
            f"   (min {self.thresholds['effective_rank_min']})",
            f"  ||mean vector||  {self.mean_norm:8.4f}"
            f"   (max {self.thresholds['mean_norm_max']}) "
            f"⚠ PROVISIONAL — v7 passes this by 0.028, so a pass here is not "
            f"evidence",
            f"  sigma20/sigma1   {self.sigma20_over_1:8.5f}"
            f"   (min {self.thresholds['sigma20_over_1_min']})",
            f"  variance top-1 {self.var_top1:.3f}  top-10 {self.var_top10:.4f}"
            f"  top-20 {self.var_top20:.6f}",
            f"  pairwise cosine  mean {self.cosine_mean:.4f}  sd {self.cosine_sd:.4f}"
            f"  p50 {self.cosine_p50:.4f}  p99 {self.cosine_p99:.4f}",
            f"  neighbour cosine 1st {self.nn1_cosine:.4f}"
            f"  200th {self.nn200_cosine:.4f}  gap {self.nn_gap:.4f}"
            f"   (min {self.thresholds['nn_gap_min']})",
        ]
        for f_ in self.failures:
            lines.append(f"  ✗ {f_}")
        return "\n".join(lines)


#: Above this many vectors the exact N x N Gram matrix is not built. At 20,000
#: it is already 3.2 GB in float64; at 1,000,000 it would be 8 TB. Below the
#: threshold the exact path runs, so small corpora give bit-identical answers to
#: the ones this module has always given.
EXACT_MAX_VECTORS = 20_000


def measure_geometry(vectors: np.ndarray,
                     thresholds: dict | None = None,
                     knn_k: int = 200,
                     *, probe_rows: int = 5_000, pair_samples: int = 5_000_000,
                     seed: int = 0) -> GeometryReport:
    """Measure, judge, and say which measurement failed which threshold.

    ``vectors`` is (N, D); it is L2-normalised here rather than assumed to be,
    because an un-normalised input silently changes every cosine below and the
    caller's model may or may not normalise.

    TWO PATHS, and the report records which one ran. Up to `EXACT_MAX_VECTORS`
    every pair is compared. Above it the SVD and the mean vector are still exact
    — they cost nothing on a tall matrix — while the pairwise-cosine
    distribution comes from `pair_samples` random pairs and the neighbourhood
    statistics from `probe_rows` random rows scored against the WHOLE corpus.
    That last point is the one that matters: neighbourhood saturation is a
    DENSITY effect, so it must be measured against every candidate a query would
    really compete with. Subsampling the corpus instead of the probes would
    understate it, and understating it is the direction that makes a bad model
    look adequate.
    """
    th = dict(DEFAULT_THRESHOLDS, **(thresholds or {}))
    # float32, not float64. Every quantity here is a cosine or a ratio of
    # singular values, accurate in float32 to ~1e-7 — six orders of magnitude
    # below the tightest threshold in the gate — and float64 doubles the cost of
    # every intermediate. At n = 1M the float64 path reached 15 GB RSS and was
    # OOM-killed twice before this was measured rather than assumed.
    V = np.asarray(vectors, dtype=np.float32)
    if V.ndim != 2:
        raise ValueError(f"expected (N, D), got {V.shape}")
    n, d = V.shape
    if n < knn_k + 1:
        # Refusing is the point: an nn_gap over fewer vectors than k is not a
        # smaller sample of the same quantity, it is a different quantity, and
        # reporting it would be a number that looks comparable and is not.
        raise ValueError(f"need more than knn_k={knn_k} vectors to measure "
                         f"neighbourhood saturation; got {n}")
    norms = np.linalg.norm(V, axis=1, keepdims=True)
    V = V / np.where(norms == 0, 1.0, norms)

    # Singular values from the D x D Gram matrix, not from an SVD of the N x D
    # one. The eigenvalues of `V.T @ V` are the squared singular values of `V`
    # exactly — no approximation — and this is O(N x D^2) work landing in a
    # 128 x 128 array, where `np.linalg.svd` on a 1,053,229 x 128 float64 matrix
    # allocates a LAPACK workspace big enough to be OOM-killed on a 31 GB
    # machine. It was, on the first run of this. ⚠ The kill arrived through a
    # `| tail` whose own exit status is 0, so the run reported SUCCESS having
    # produced two of its three results and written no output file — a pipe
    # turning SIGKILL into exit 0 is the same shape as the hash of nothing.
    gram = (V.T @ V).astype(np.float64)     # 128 x 128: promote the REDUCTION,
    ev = np.clip(np.linalg.eigvalsh(gram)[::-1], 0.0, None)   # not the corpus
    s = np.sqrt(ev)
    ev = ev / ev.sum()
    effective_rank = float(1.0 / np.sum(ev ** 2))     # participation ratio

    rng = np.random.default_rng(seed)
    if n <= EXACT_MAX_VECTORS:
        method = "exact"
        n_probe = n
        C = V @ V.T
        iu = np.triu_indices(n, 1)
        cos = C[iu]
        np.fill_diagonal(C, -2.0)
        # Partition rather than sort: only the k largest per row are needed, and
        # a full sort of every row is otherwise the dominant cost.
        part = np.partition(C, -knn_k, axis=1)[:, -knn_k:]
    else:
        method = f"sampled(probe_rows={probe_rows:,}, pair_samples={pair_samples:,})"
        # Chunked, because `V[i]` for 5M indices materialises a 5M x D float64
        # array — 5.1 GB at D=128, twice over. The first run of this at n=1M
        # reached 13 GB RSS on a 31 GB machine doing nothing but gathering rows
        # it was about to reduce away.
        parts = []
        for start in range(0, pair_samples, 500_000):
            m = min(500_000, pair_samples - start)
            i = rng.integers(0, n, m)
            j = rng.integers(0, n, m)
            keep = i != j
            parts.append(np.einsum("ij,ij->i", V[i[keep]], V[j[keep]]))
        cos = np.concatenate(parts)
        probe = rng.choice(n, size=min(probe_rows, n), replace=False)
        n_probe = len(probe)
        rows = []
        # The score block is len(block) x n float64; at n = 1M a 256-row block
        # is 2.1 GB. Size the block to the corpus so the peak stays ~0.5 GB
        # rather than depending on the caller's machine having headroom.
        # ~64M floats per score block, i.e. ~256 MB in float32 and the same
        # again for `np.partition`'s copy. The first version of this line
        # multiplied by 8 and clamped to 256, so it returned 256 for every
        # corpus size and never shrank — the arithmetic looked adaptive and was
        # constant.
        block_rows = int(max(16, min(256, 64_000_000 // max(n, 1))))
        for start in range(0, n_probe, block_rows):
            block = probe[start:start + block_rows]
            sims = V[block] @ V.T
            # A probe row is its own nearest neighbour; mask it, not the
            # diagonal, because `probe` indexes into the full corpus.
            sims[np.arange(len(block)), block] = -2.0
            rows.append(np.partition(sims, -knn_k, axis=1)[:, -knn_k:])
        part = np.vstack(rows)
    nn1 = float(part.max(axis=1).mean())
    nn_k = float(part.min(axis=1).mean())

    rep = GeometryReport(
        n_vectors=n, dim=d,
        effective_rank=effective_rank,
        mean_norm=float(np.linalg.norm(V.mean(axis=0))),
        sigma20_over_1=float(s[19] / s[0]) if d >= 20 else float("nan"),
        var_top1=float(ev[0]), var_top10=float(ev[:10].sum()),
        var_top20=float(ev[:20].sum()),
        cosine_mean=float(cos.mean()), cosine_sd=float(cos.std()),
        cosine_p50=float(np.percentile(cos, 50)),
        cosine_p99=float(np.percentile(cos, 99)),
        nn1_cosine=nn1, nn200_cosine=nn_k, nn_gap=nn1 - nn_k,
        thresholds=th, method=method, n_probe_rows=n_probe,
    )

    if rep.effective_rank < th["effective_rank_min"]:
        rep.failures.append(
            f"effective rank {rep.effective_rank:.2f} < {th['effective_rank_min']} "
            f"of {d} — the space is collapsed; {d - round(rep.effective_rank)} "
            f"dimensions carry nothing")
    if rep.mean_norm > th["mean_norm_max"]:
        rep.failures.append(
            f"||mean vector|| {rep.mean_norm:.4f} > {th['mean_norm_max']} — every "
            f"vector shares a large common component, so cosine is dominated by "
            f"the mean rather than by the name")
    if rep.sigma20_over_1 < th["sigma20_over_1_min"]:
        rep.failures.append(
            f"sigma20/sigma1 {rep.sigma20_over_1:.5f} < {th['sigma20_over_1_min']} "
            f"— the spectrum falls off a cliff at component 20")
    if rep.cosine_p50 > th["p50_cosine_max"]:
        rep.failures.append(
            f"median pairwise cosine {rep.cosine_p50:.4f} > {th['p50_cosine_max']} "
            f"— half of all RANDOM pairs already score this high, so no similarity "
            f"threshold can separate matches from non-matches")
    if rep.nn_gap < th["nn_gap_min"]:
        rep.failures.append(
            f"neighbourhood saturated: 1st neighbour {rep.nn1_cosine:.4f} vs "
            f"{knn_k}th {rep.nn200_cosine:.4f}, gap {rep.nn_gap:.4f} < "
            f"{th['nn_gap_min']} — a k={knn_k} KNN returns {knn_k} equally-good "
            f"answers and ranking within it is noise")
    return rep
