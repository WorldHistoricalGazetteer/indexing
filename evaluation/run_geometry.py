"""Run the geometry gate against a Symphonym checkpoint over real toponyms.

  python -m evaluation.run_geometry --names <file.txt|.jsonl> [--model-dir hf]

The names file is one toponym per line, or JSONL with a "name" (and optional
"lang"/"script") key. Prints the report and exits non-zero if the gate fails —
so it can be wired into a training run's tail rather than read by a human who
may or may not look.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_names(path: Path, limit: int) -> list[tuple[str, str]]:
    items, seen = [], set()
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):
                row = json.loads(line)
                name, lang = row.get("name", ""), row.get("lang") or "und"
            else:
                name, lang = line, "und"
            if name and name not in seen:
                seen.add(name)
                items.append((name, lang))
            if limit and len(items) >= limit:
                break
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", required=True)
    ap.add_argument("--model-dir", default="hf")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--limit", type=int, default=6000)
    ap.add_argument("--batch-size", type=int, default=2048)
    ap.add_argument("--json-out")
    args = ap.parse_args()

    import numpy as np
    sys.path.insert(0, str(Path(args.model_dir).resolve()))
    from processing.device import resolve_device
    from inference import SymphonymModel

    device = resolve_device(args.device, purpose="geometry gate")
    items = load_names(Path(args.names), args.limit)
    model = SymphonymModel(model_dir=Path(args.model_dir), device=device)
    vecs = np.vstack([model.batch_embed(items[i:i + args.batch_size])
                      for i in range(0, len(items), args.batch_size)])

    from evaluation.geometry import measure_geometry
    rep = measure_geometry(vecs)
    print(f"[geometry] corpus {args.names} — {len(items):,} distinct names, "
          f"model {args.model_dir} on {device}")
    print(rep.summary())
    if args.json_out:
        Path(args.json_out).write_text(rep.to_json())
    return 0 if rep.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
