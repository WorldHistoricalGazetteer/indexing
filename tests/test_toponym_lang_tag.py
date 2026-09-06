"""No authority may emit a toponym_id with an empty language tag.

THE FAILURE THIS PINS. The `extract_namespace` ingest pipeline — the places
index's `default_pipeline`, so it runs on every write — discards any toponym
whose language segment is empty:

    if (origName.length() == 0 || lang.length() == 0) { continue; }

⚠ The discard happens INSIDE Elasticsearch, after the indexer has handed the
document over. The write reports success with a full document count and zero
errors, and the toponyms are simply not there. No stage-level check can see it,
which is why it survived from July to September: the August run logged
`docs_in_source: 2,991,143, docs_indexed: 2,991,143, errors: 0`.

Measured 6 Sep 2026 over ~40k staged docs per namespace, all 28 namespaces:

    tgn    100,651 toponyms   60,290 empty-lang   59.90%   <- dropped
    other 27 namespaces                        0    0.00%

and 1,277,683 tgn places carried `toponyms: []` in the live index because every
one of their Getty terms was untagged.
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class ToponymLangTagTest(unittest.TestCase):

    def test_tgn_defaults_untagged_terms_to_und(self):
        src = (REPO / "authorities" / "tgn-places.py").read_text(encoding="utf-8")
        block = src[src.index("def make_doc"):src.index("# Skip unnamed records")]
        self.assertIn('lang = lang or "und"', block,
                      "tgn builds toponym_id from Getty's xml:lang, which is "
                      "routinely absent; without a default the ingest pipeline "
                      "silently discards the toponym")
        self.assertLess(block.index('lang = lang or "und"'),
                        block.index('toponym_id = f"{name}@{lang}"'),
                        "the default must be applied BEFORE the id is built")

    #: Sites that build a toponym_id from a language variable with no visible
    #: default. They are NOT defects: the 6 Sep 2026 scan measured every one of
    #: their namespaces at 0 empty-lang toponyms over ~40k staged docs each, so
    #: the variable is non-empty in practice. Static analysis cannot show that —
    #: only the measurement can — so this records the measured-clean set and
    #: fails if it GROWS. A new entry means new code that has never been
    #: measured, and the cost of being wrong is silent data loss inside ES.
    KNOWN_CLEAN = {
        "pleiades-places.py", "wikidata-places.py", "ottgaz-places.py",
        "ottnfs-places.py", "vob_common.py",
    }

    def test_no_new_authority_builds_an_unguarded_at_lang_id(self):
        """Guards the family. Fails on a NEW unguarded site, not the measured ones.

        ⚠ This is a tripwire, not a proof. If one of the KNOWN_CLEAN sources
        starts emitting untagged names, nothing here fires — the defect is
        invisible to static inspection and was found by scanning staged data.
        Re-run that scan after any authority's source format changes.
        """
        offenders = set()
        pat = re.compile(r'f"\{[a-z_]*name[a-z_]*\}@\{([a-z_]+)\}"')
        for path in (REPO / "authorities").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for m in pat.finditer(text):
                var = m.group(1)
                window = text[max(0, m.start() - 600):m.start()]
                if f'{var} = {var} or' in window or f'{var} or "' in window:
                    continue
                if re.search(rf'{var}\s*=\s*["\'][a-z]{{2,3}}["\']', window):
                    continue
                offenders.add(path.name)
        new = offenders - self.KNOWN_CLEAN
        self.assertEqual(new, set(),
                         f"unguarded toponym_id language variable in {sorted(new)} — "
                         f"if that source can emit an untagged name, the ingest "
                         f"pipeline discards the toponym with no error. Either "
                         f"default it to 'und' or scan its staged output and add "
                         f"it to KNOWN_CLEAN with the measurement.")


if __name__ == "__main__":
    unittest.main()
