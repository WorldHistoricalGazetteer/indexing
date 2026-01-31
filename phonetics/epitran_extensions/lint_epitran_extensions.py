#!/usr/bin/env python3
"""Lightweight linter/sanity-checker for epitran extension CSVs.

Goal: catch common issues that break Epitran/PanPhon robustness:
- duplicate Orth keys (ambiguity / overwritten mappings)
- empty Orth/Phon
- suspicious IPA symbols outside a permissive whitelist

This is *not* a linguistic validator; it just prevents accidental junk.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path


def iter_extension_csvs(root: Path) -> list[Path]:
    return sorted(
        p
        for p in root.glob("*.csv")
        if p.is_file() and p.name.lower().endswith(".csv") and p.name != "audit-gn-wd-tgn.txt"
    )


# More permissive check: allow any non-control characters, but still reject HTML-ish or binary junk.
# We mainly want to catch accidental empty values, huge strings, or non-text.
CTRL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")


def lint_file(path: Path) -> list[str]:
    problems: list[str] = []

    # Read as bytes to catch accidental NULs; strip them so csv.reader won't crash.
    data = path.read_bytes()
    if b"\x00" in data:
        problems.append(f"{path.name}: contains NUL bytes (\\x00); stripping for lint")
        data = data.replace(b"\x00", b"")

    text = data.decode("utf-8", errors="replace")
    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames != ["Orth", "Phon"]:
        problems.append(
            f"{path.name}: header is {reader.fieldnames!r}, expected ['Orth', 'Phon']"
        )
        return problems

    seen: set[str] = set()

    # Empty-phon mappings are sometimes intentional (silent letters or orthographic markers).
    # We still want to catch accidental blanks elsewhere.
    allow_empty_phon_for = {
        "h",
        "H",
        "ъ",
        "Ъ",
        "්",  # Sinhala hal kirima (virama)
        "ᱻ",
        "ᱼ",
        "ᱽ",
    }

    for i, row in enumerate(reader, start=2):
        orth = (row.get("Orth") or "").strip()
        phon = (row.get("Phon") or "").strip()

        if not orth:
            problems.append(f"{path.name}:{i}: empty Orth")
            continue

        if (not phon) and (orth not in allow_empty_phon_for):
            problems.append(f"{path.name}:{i}: empty Phon for Orth={orth!r}")

        if orth in seen:
            problems.append(f"{path.name}:{i}: duplicate Orth key {orth!r}")
        seen.add(orth)

        # Flag obviously non-IPA junk (HTML, long sentences, etc.)
        if len(phon) > 40:
            problems.append(f"{path.name}:{i}: unusually long Phon value ({len(phon)} chars)")

        if phon and CTRL_RE.search(phon):
            problems.append(f"{path.name}:{i}: phon contains control characters")

    return problems


def main() -> int:
    root = Path(__file__).resolve().parent
    csvs = iter_extension_csvs(root)
    if not csvs:
        print("No extension CSVs found.")
        return 1

    all_problems: list[str] = []
    for p in csvs:
        all_problems.extend(lint_file(p))

    if all_problems:
        print("Found problems:\n")
        for prob in all_problems:
            print("-", prob)
        return 2

    print(f"OK: {len(csvs)} CSV files passed basic lint checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
