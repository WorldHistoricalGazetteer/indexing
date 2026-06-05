#!/usr/bin/env python
"""Backfill Symphonym embeddings for toponyms that lack them.

The toponyms ES index stores only the Symphonym ``embedding`` (128-d byte
dense_vector) + ``embedding_version`` — no IPA/PanPhon (those are training-time
intermediates). Inference is name-only (Student model, no G2P): the very same
``SymphonymModel.embed(name, lang)`` the gateway uses for live fuzzy queries.
So giving an embedding-less toponym a vector is just: embed its name, quantise,
write it back. Until then such toponyms are findable by exact/prefix/wildcard
but NOT by fuzzy/phonetic KNN.

Why three phases (bridged by the shared ``/vast`` filesystem):

  prod ES is firewalled to **pitt**'s localhost; the **GPU** is on **CRC**, and
  the two hosts can't reach each other's services. So:

    export   (run on PITT)   prod ES  → input.jsonl   {toponym_id, name, lang}
    compute  (run on CRC GPU) input    → emb.jsonl     {toponym_id, embedding[128]}
    index    (run on PITT)   emb.jsonl → bulk-update prod ES (embedding + version)

Example (ns=ofs, the 2026-06-05 incremental add):

    # 1. on pitt
    python -m phonetics.inference.backfill_embeddings export \
        --es-host http://localhost:9201 --namespace ofs \
        --out /vast/ishi/staged/ofs/backfill/input.jsonl

    # 2. on a CRC GPU node (sbatch -M gpu --partition a100 --gres=gpu:1)
    python -m phonetics.inference.backfill_embeddings compute \
        --in  /vast/ishi/staged/ofs/backfill/input.jsonl \
        --out /vast/ishi/staged/ofs/backfill/embeddings.jsonl --device cuda

    # 3. on pitt (writes prod)
    python -m phonetics.inference.backfill_embeddings index \
        --es-host http://localhost:9201 \
        --in /vast/ishi/staged/ofs/backfill/embeddings.jsonl --embedding-version 7
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EMBEDDING_DIM = 128
DEFAULT_ES_PASSWORD_FILE = "/ix1/ishi/es/config/elastic.password"


# ---------------------------------------------------------------------------
# ES client (basic_auth from a password file — never embed the pw in the URL)
# ---------------------------------------------------------------------------

def _es_client(es_host: str, password_file: str | None):
    from elasticsearch import Elasticsearch
    kwargs = {"request_timeout": 300}
    pf = Path(password_file) if password_file else None
    if pf and pf.exists():
        try:
            pw = pf.read_text().strip()
            kwargs["basic_auth"] = ("elastic", pw)
        except PermissionError:
            pass  # unreadable secrets dir → try unauthenticated
    return Elasticsearch(es_host, **kwargs)


# ---------------------------------------------------------------------------
# Phase: export  (PITT — reads prod ES)
# ---------------------------------------------------------------------------

def cmd_export(args) -> None:
    from elasticsearch.helpers import scan

    es = _es_client(args.es_host, args.es_password_file)
    must_not = [{"exists": {"field": "embedding"}}]
    must = []
    if args.namespace:
        must.append({"term": {"namespaces": args.namespace}})
    query = {"bool": {"must": must, "must_not": must_not}} if (must or must_not) else {"match_all": {}}

    total = es.count(index=args.index, query=query)["count"]
    print(f"[export] {args.index}: {total:,} toponyms lack an embedding"
          f"{f' (namespace={args.namespace})' if args.namespace else ''}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    t0 = time.time()
    with out.open("w", encoding="utf-8") as fh:
        for hit in scan(es, index=args.index, query={"query": query},
                        _source=["name", "lang"], size=args.batch_size,
                        preserve_order=False):
            src = hit.get("_source", {})
            name = (src.get("name") or "").strip()
            if not name:
                continue  # nothing to embed
            fh.write(json.dumps({
                "toponym_id": hit["_id"],
                "name": name,
                "lang": (src.get("lang") or "und"),
            }, ensure_ascii=False) + "\n")
            written += 1
            if written % 50_000 == 0:
                print(f"[export]   {written:,} written ({written/(time.time()-t0):.0f}/s)")
    print(f"[export] done: {written:,} rows → {out}  ({time.time()-t0:.0f}s)")


# ---------------------------------------------------------------------------
# Phase: compute  (CRC GPU — no ES)
# ---------------------------------------------------------------------------

def _load_model(device: str, model_dir: str | None):
    """Load the SAME SymphonymModel the gateway uses, but on the chosen device.

    Reuses gateway.symphonym._resolve_model_dir() so the assembled model_dir
    (config + vocab + weights) and therefore the embedding space match the
    indexed vectors exactly.
    """
    if model_dir:
        md = Path(model_dir)
    else:
        from gateway.symphonym import _resolve_model_dir
        md = _resolve_model_dir()
    # hf/inference.py exposes SymphonymModel; add hf/ to sys.path like the gateway.
    hf_dir = Path(__file__).resolve().parents[2] / "hf"
    if str(hf_dir) not in sys.path:
        sys.path.insert(0, str(hf_dir))
    from inference import SymphonymModel
    print(f"[compute] loading SymphonymModel from {md} on {device} ...")
    return SymphonymModel(model_dir=md, device=device)


def _quantize(emb) -> list[int]:
    import numpy as np
    q = np.round(np.asarray(emb) * 127.0).clip(-128, 127).astype(np.int8)
    return q.tolist()


def _stream_batches(path: str, size: int):
    """Yield lists of <=size parsed rows, streaming the file (bounded memory).

    Scales to the ~16.5M index-wide backlog without materialising all rows.
    """
    batch = []
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            batch.append(json.loads(line))
            if len(batch) >= size:
                yield batch
                batch = []
    if batch:
        yield batch


def cmd_compute(args) -> None:
    model = _load_model(args.device, args.model_dir)
    print(f"[compute] streaming {args.inp} (batch={args.batch_size})")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = 0
    t0 = time.time()
    with out.open("w", encoding="utf-8") as fh:
        for chunk in _stream_batches(args.inp, args.batch_size):
            embs = model.batch_embed([(r["name"], r["lang"]) for r in chunk])
            for r, emb in zip(chunk, embs):
                vec = _quantize(emb)
                if len(vec) != EMBEDDING_DIM:
                    raise ValueError(f"expected {EMBEDDING_DIM}-d, got {len(vec)} for {r['toponym_id']!r}")
                fh.write(json.dumps({"toponym_id": r["toponym_id"], "embedding": vec}) + "\n")
            done += len(chunk)
            if done % 100_000 == 0:
                print(f"[compute]   {done:,} embedded ({done/(time.time()-t0):.0f}/s)", flush=True)
    print(f"[compute] done: {done:,} embeddings → {out}  ({time.time()-t0:.0f}s)")


# ---------------------------------------------------------------------------
# Phase: index  (PITT — writes prod ES)
# ---------------------------------------------------------------------------

def cmd_index(args) -> None:
    from elasticsearch.helpers import streaming_bulk

    es = _es_client(args.es_host, args.es_password_file)
    now = datetime.now(timezone.utc).isoformat()

    def actions():
        for line in Path(args.inp).open(encoding="utf-8"):
            if not line.strip():
                continue
            rec = json.loads(line)
            vec = rec["embedding"]
            if len(vec) != EMBEDDING_DIM:
                raise ValueError(f"bad embedding length {len(vec)} for {rec['toponym_id']!r}")
            yield {
                "_op_type": "update",
                "_index": args.index,
                "_id": rec["toponym_id"],
                "doc": {
                    "embedding": vec,
                    "embedding_version": args.embedding_version,
                    "indexed_at": now,
                },
            }

    if args.dry_run:
        n = sum(1 for _ in Path(args.inp).open(encoding="utf-8") if _.strip())
        print(f"[index] DRY-RUN: would update {n:,} docs in {args.index} "
              f"(embedding + embedding_version={args.embedding_version}). No writes.")
        return

    ok = errs = 0
    t0 = time.time()
    for success, info in streaming_bulk(es.options(request_timeout=300), actions(),
                                        chunk_size=args.batch_size,
                                        raise_on_error=False, max_retries=3,
                                        initial_backoff=2):
        if success:
            ok += 1
        else:
            errs += 1
            if errs <= 5:
                print(f"[index]   error: {info}")
        if (ok + errs) % 20_000 == 0:
            print(f"[index]   {ok:,} ok / {errs:,} err ({(ok+errs)/(time.time()-t0):.0f}/s)")
    es.indices.refresh(index=args.index)
    print(f"[index] done: ok={ok:,} errors={errs:,}  ({time.time()-t0:.0f}s)  refreshed {args.index}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)

    pe = sub.add_parser("export", help="prod ES → input.jsonl (run on pitt)")
    pe.add_argument("--es-host", required=True)
    pe.add_argument("--es-password-file", default=DEFAULT_ES_PASSWORD_FILE)
    pe.add_argument("--index", default="toponyms")
    pe.add_argument("--namespace", help="restrict to toponyms attested by this namespace")
    pe.add_argument("--out", required=True)
    pe.add_argument("--batch-size", type=int, default=5000)
    pe.set_defaults(func=cmd_export)

    pc = sub.add_parser("compute", help="input.jsonl → embeddings.jsonl (run on CRC GPU)")
    pc.add_argument("--in", dest="inp", required=True)
    pc.add_argument("--out", required=True)
    pc.add_argument("--device", default="cuda", help="cuda (default) or cpu")
    pc.add_argument("--model-dir", help="override Symphonym model dir (else gateway resolver)")
    pc.add_argument("--batch-size", type=int, default=1024)
    pc.set_defaults(func=cmd_compute)

    pi = sub.add_parser("index", help="embeddings.jsonl → bulk-update prod ES (run on pitt)")
    pi.add_argument("--es-host", required=True)
    pi.add_argument("--es-password-file", default=DEFAULT_ES_PASSWORD_FILE)
    pi.add_argument("--index", default="toponyms")
    pi.add_argument("--in", dest="inp", required=True)
    pi.add_argument("--embedding-version", type=int, required=True,
                    help="MUST match the index's prevailing version (currently 7)")
    pi.add_argument("--batch-size", type=int, default=2000)
    pi.add_argument("--dry-run", action="store_true")
    pi.set_defaults(func=cmd_index)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
