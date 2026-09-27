"""Negative controls for a round-trip check: can it fail, and on what?

    python3 controls.py <graph.nt> <workdir> -- <check command, with {nt} and {out} placeholders>

For every predicate in the graph, corrupt ONE value it carries (the first triple using it), run the
check command on the corrupted graph, and record whether the check failed (non-zero exit). A
predicate whose corruption the check does not notice is one the comparison never reads: either a
hole to close, or a value the converter declares it does not verify. A control that corrupts a
value the comparison never reads proves nothing (whg3-6a, 27 Sep 2026), hence one per predicate.

Literal objects are corrupted in place (a string gains a character, a number changes value); IRI
objects are pointed elsewhere. List members (rdf:first) are reached through their predicate, so a
coordinate pair inside reprPoint is exercised as rdf:first.
"""
import re, subprocess, sys, collections
from pathlib import Path

LIT = re.compile(r'^(\S+) (\S+) ("(?:[^"\\]|\\.)*")(\^\^<[^>]+>|@[\w-]+)? \.$')
IRI = re.compile(r'^(\S+) (\S+) <([^>]+)> \.$')


def corrupt(line):
    m = LIT.match(line)
    if m:
        s, p, lit, tail = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        body = lit[1:-1]
        if "double" in tail or "decimal" in tail or "integer" in tail:
            new = str(float(body) + 0.5) if "integer" not in tail else str(int(body) + 1)
        else:
            new = body + "Z"
        return f'{s} {p} "{new}"{tail} .'
    m = IRI.match(line)
    if m:
        return f"{m.group(1)} {m.group(2)} <{m.group(3)}-corrupted> ."
    return None  # blank-node object: nothing to point elsewhere without changing structure


def main():
    nt, work = sys.argv[1], Path(sys.argv[2])
    cmd = sys.argv[sys.argv.index("--") + 1:]
    work.mkdir(parents=True, exist_ok=True)
    lines = open(nt).read().splitlines()
    first = collections.OrderedDict()
    for i, l in enumerate(lines):
        p = l.split(" ", 2)[1] if l.count(" ") >= 2 else None
        if p and p not in first and corrupt(l):
            first[p] = i
    base = subprocess.run([c.format(nt=nt, out=work / "clean") for c in cmd], capture_output=True, text=True)
    if base.returncode != 0:
        sys.exit(f"the clean graph already fails the check; controls mean nothing:\n{base.stdout[-2000:]}")
    missed = []
    for p, i in first.items():
        bad = lines[:]; bad[i] = corrupt(lines[i])
        f = work / "bad.nt"; f.write_text("\n".join(bad) + "\n")
        r = subprocess.run([c.format(nt=f, out=work / "bad") for c in cmd], capture_output=True, text=True)
        print(f"  {'caught' if r.returncode else 'MISSED'}  {p}")
        if not r.returncode:
            missed.append(p)
    print(f"{len(first) - len(missed)} of {len(first)} predicates: corrupting one value made the check fail")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
