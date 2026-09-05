"""Gate 3 — recall@k and MRR over the real haystack, per script pair.

THE MEASUREMENT. For each query name, rank every name in the haystack and find
where the query's true cross-script partner lands. recall@{1,10,100,200} and
MRR, **reported per script pair with its own denominator** — a corpus-wide
average is dominated by LATIN→LATIN and hides exactly the cross-script cases the
model exists for. k=200 is not arbitrary: it is the gateway's KNN pool size, so
recall@200 is the ceiling on what any downstream re-ranking can recover.

THE GUARD THAT MATTERS MOST. If a query's partner is not in the haystack, its
rank is undefined and every naive implementation scores it 0 — so a corpus bug
reads as a model failure, uniformly, across every scorer, and looks like a
finding. `check_partners_present` raises instead. This is the same shape as the
old pairs test (a check that cannot fail) inverted into a check that cannot
silently pass.

COMPARABILITY OVER POWER. Every scorer is evaluated on the SAME queries and the
SAME haystack. Symphonym could afford far more queries than edit distance can
(one matmul against 1M vectors versus 1M string comparisons), and using that
headroom would produce two numbers that are not comparable. The query count is
therefore set by the slowest scorer and stated in the report.
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

DEFAULT_KS = (1, 10, 100, 200)


class PartnerMissing(AssertionError):
    """A query's true partner is not in the haystack, so its rank is undefined."""


def check_partners_present(queries, haystack_names: set[str]) -> None:
    """Fail loudly rather than score an unreachable partner as a miss.

    `queries` is an iterable of objects with `.partner`. A missing partner is a
    corpus defect; scoring it 0 would depress every scorer identically and be
    indistinguishable from a genuine, uniform model failure.
    """
    missing = [q for q in queries if q.partner not in haystack_names]
    if missing:
        ex = ", ".join(repr(q.partner) for q in missing[:5])
        raise PartnerMissing(
            f"{len(missing):,} of {len(list(queries)):,} queries have a partner "
            f"absent from the haystack ({ex}...). Their rank is undefined and "
            f"scoring them as misses would depress every scorer equally, which "
            f"reads as a model result. Inject the partners (build_corpus does "
            f"this and counts them) or drop these queries and say how many.")


def recall_at_k(ranks: list[int | None], ks=DEFAULT_KS) -> dict:
    """recall@k and MRR over ranks (1-based; None = not found in the scored pool).

    A `None` rank is a genuine miss — the partner was in the haystack and the
    scorer did not surface it within the pool it returned — as distinct from the
    partner being absent, which `check_partners_present` has already refused.
    """
    n = len(ranks)
    if not n:
        return {"n": 0, "mrr": None} | {f"recall@{k}": None for k in ks}
    out = {"n": n}
    for k in ks:
        hit = sum(1 for r in ranks if r is not None and r <= k)
        out[f"recall@{k}"] = hit / n
        out[f"hits@{k}"] = hit          # the numerator, beside the rate
    out["mrr"] = sum(1.0 / r for r in ranks if r is not None) / n
    out["not_found"] = sum(1 for r in ranks if r is None)
    return out


def by_script_pair(queries, ranks: list[int | None], ks=DEFAULT_KS) -> dict:
    """Per-script-pair breakdown. Cells with no queries are absent, not zero."""
    groups: dict[str, list] = defaultdict(list)
    for q, r in zip(queries, ranks):
        groups["→".join(q.script_pair)].append(r)
    return {k: recall_at_k(v, ks) for k, v in sorted(groups.items())}


def ranks_from_scores(score_matrix: np.ndarray, target_idx: np.ndarray,
                      pool: int | None = None) -> list[int | None]:
    """1-based rank of each row's target, optionally truncated to a pool of `pool`.

    Ties are resolved PESSIMISTICALLY — the target is placed after every
    candidate scoring the same. In a collapsed embedding space ties are common
    (that is what neighbourhood saturation means), and optimistic tie handling
    would report a saturated model as a good one.
    """
    out: list[int | None] = []
    for row, tgt in zip(score_matrix, target_idx):
        t = row[tgt]
        rank = int((row > t).sum() + (row == t).sum())   # pessimistic: ties count
        if pool is not None and rank > pool:
            out.append(None)
        else:
            out.append(rank)
    return out


def embed_names(model, names: list[str], langs: list[str], batch: int = 4096) -> np.ndarray:
    vecs = [model.batch_embed(list(zip(names[i:i + batch], langs[i:i + batch])))
            for i in range(0, len(names), batch)]
    V = np.vstack(vecs).astype(np.float32)
    n = np.linalg.norm(V, axis=1, keepdims=True)
    return V / np.where(n == 0, 1.0, n)


def rank_by_embedding(query_vecs: np.ndarray, hay_vecs: np.ndarray,
                      target_idx: np.ndarray, *, pool: int = 200,
                      chunk: int = 512) -> list[int | None]:
    """Ranks by cosine, chunked so the 20k x 1M score matrix is never materialised."""
    ranks: list[int | None] = []
    for i in range(0, len(query_vecs), chunk):
        sims = query_vecs[i:i + chunk] @ hay_vecs.T
        ranks.extend(ranks_from_scores(sims, target_idx[i:i + chunk], pool=pool))
    return ranks


def rank_by_string(queries_names: list[str], hay_names: list[str],
                   target_idx: np.ndarray, scorer, *, pool: int = 200,
                   chunk: int = 256, workers: int = -1) -> list[int | None]:
    """Ranks by a rapidfuzz scorer, via `process.cdist` for the C++/SIMD path.

    ⚠ This is the slow scorer and it is what sets the query budget. 20,000
    queries against 1,000,000 candidates is 2e10 string comparisons; the point
    of stating that here is that anyone raising the query count should expect it
    to be the cost, not the embedding pass.
    """
    from rapidfuzz import process
    ranks: list[int | None] = []
    for i in range(0, len(queries_names), chunk):
        sims = process.cdist(queries_names[i:i + chunk], hay_names,
                             scorer=scorer, workers=workers, dtype=np.float32)
        ranks.extend(ranks_from_scores(sims, target_idx[i:i + chunk], pool=pool))
    return ranks


def balanced_query_sample(queries, per_pair: int, rng) -> list:
    """Up to `per_pair` queries from each script pair — the query-balancing step.

    Under-full pairs are kept at whatever size they have rather than dropped;
    a pair with 40 queries is a weak measurement and an honest one, and its
    denominator says so.
    """
    groups: dict[tuple, list] = defaultdict(list)
    for q in queries:
        groups[q.script_pair].append(q)
    out = []
    for pair, items in sorted(groups.items()):
        out.extend(items if len(items) <= per_pair else rng.sample(items, per_pair))
    return out
