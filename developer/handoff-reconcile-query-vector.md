# Handoff — accept a client-supplied query_vector in the gateway reconcile (Symphonym)

**Status:** implemented in this repo (`gateway/`), NOT yet deployed on CRC. Requested 2026-07-06.
**Why:** the Gazetteer Workbench now computes a **language-conditioned int8 Symphonym embedding per
row in the browser** and sends it with each reconcile query, to **offload the server embed** and to
let the client control the query language (the gateway otherwise embeds as `lang="und"`).

## Change (small, additive)
- `ReconcileRequest` (`gateway/reconcile.py`) gains `query_vector: Optional[list[int]]` (128 int8 values).
- `_build_phonetic_knn` / `es_helpers.build_phonetic_knn` gains a `query_vector` param, forwarded to
  `symphonym.build_knn_query`.
- `symphonym.build_knn_query` uses the supplied vector directly and **skips `embed()`/`quantize_to_byte()`**
  when `query_vector` is provided; otherwise embeds the query text as before.
- Discovery Step 1 passes `req.query_vector` through (`reconcile.py:307`).

## Client contract (must match the index)
The browser quantises exactly like the gateway: `round(emb * 127)` clipped to `[-128, 127]`, where
`emb` is the fp32 **L2-normalised** 128-d Symphonym embedding — i.e. `symphonym.quantize_to_byte`
(`gateway/symphonym.py:155`). The vector is a plain JSON array of 128 ints. Order/dims must match the
`toponyms.embedding` field (128-d byte, cosine).

## Deploy
Gateway runs on CRC (pitt), separate from the whg web app. Redeploy the gateway service there to
activate. **Safe to deploy any time / no ordering constraint:** whg3 already sends `query_vector`, and
until this lands the gateway simply ignores the unknown field (Pydantic default) and embeds server-side
— identical results, just without the offload.

## Verify
POST `/api/reconcile` with `{"query":"London","mode":"phonetic","query_vector":[...128 ints...]}` and
confirm results match the no-vector call for the same name/lang, and that the server did not embed
(add a debug log in `build_knn_query` if needed).
