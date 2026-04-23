#!/usr/bin/env python3
"""Tiny harness for boundary merge stage.

Creates a synthetic staged namespace with two docs and one boundary patch,
invokes ``processing.boundary_merge`` and validates merged output.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="whg-boundary-merge-") as tmp:
        base = Path(tmp)
        ns = "ohm"

        extract_dir = base / ns / "extract"
        boundary_dir = base / ns / "boundary"
        extract_dir.mkdir(parents=True, exist_ok=True)
        boundary_dir.mkdir(parents=True, exist_ok=True)

        docs = [
            {
                "place_id": "ohm:r1",
                "title": "Old Boundary",
                "geometries": [],
                "boundary": "administrative",
            },
            {
                "place_id": "ohm:r2",
                "title": "Untouched",
                "geometries": [],
            },
        ]
        with (extract_dir / "places.jsonl").open("w", encoding="utf-8") as fh:
            for d in docs:
                fh.write(json.dumps(d) + "\n")

        patch = {
            "place_id": "ohm:r1",
            "update_doc": {
                "geometries": [
                    {
                        "geom": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
                        "repr_point": {"lon": 0.5, "lat": 0.5},
                    }
                ],
                "boundary": "4",
            },
            "upsert_doc": {
                "place_id": "ohm:r1",
                "title": "Boundary Upsert",
                "geometries": [],
            },
        }
        with (boundary_dir / "places.boundary.jsonl").open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(patch) + "\n")

        env = os.environ.copy()
        env["STAGED_BASE_DIR"] = str(base)

        cmd = [
            "python3",
            "-m",
            "processing.boundary_merge",
            "--run-id",
            "test-run",
            "--namespace",
            ns,
        ]
        subprocess.run(cmd, env=env, check=True)

        merged_jsonl = base / ns / "boundary_merged" / "places.jsonl"
        assert merged_jsonl.exists(), "Merged JSONL output not created"

        merged_docs = []
        with merged_jsonl.open("r", encoding="utf-8") as fh:
            for line in fh:
                merged_docs.append(json.loads(line))

        assert len(merged_docs) == 2, f"Expected 2 merged docs, got {len(merged_docs)}"
        by_id = {d["place_id"]: d for d in merged_docs}
        assert by_id["ohm:r1"].get("boundary") == "4", "Boundary patch not applied"
        assert by_id["ohm:r1"].get("geometries"), "Geometry patch not applied"
        assert by_id["ohm:r2"].get("title") == "Untouched", "Unpatched doc changed unexpectedly"

        print("OK: boundary merge harness passed")


if __name__ == "__main__":
    main()

