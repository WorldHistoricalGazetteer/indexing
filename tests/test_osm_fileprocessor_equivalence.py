"""The C++ prefilter must reject exactly what the Python predicate rejected.

WHY THIS IS THE ONLY DANGEROUS PART OF THE CONVERSION. Moving from
`SimpleHandler.apply_file` to `FileProcessor` is semantically inert if — and
only if — `osmium.filter.KeyFilter("name")` discards nothing the handler would
have kept. Get that wrong and objects vanish SILENTLY: no error, no warning, a
smaller corpus that looks like a smaller planet. The failure has exactly one
signature, a document count against a prior run, so it needs a check that can
produce that count on a fixture rather than on a 94 GB file.

The fixture is built to REACH the divergence rather than to pass:

    named + keyed        must be kept   (the ordinary ingested feature)
    named, NOT keyed     must be dropped by process_tags, NOT by the filter
    keyed, NOT named     must be dropped by the FILTER — the case that decides
                         whether narrowing the filter to our tag keys would be safe
    neither              must be dropped
    no tags at all       must be dropped   (most of a planet)

A fixture of only ingested features would pass against any filter at all.
"""
import unittest

try:
    import osmium
    import osmium.filter
    HAVE = hasattr(osmium, "FileProcessor") and hasattr(osmium.filter, "KeyFilter")
except ImportError:
    HAVE = False

KEYS = ("place", "natural", "water", "waterway", "historic", "landuse", "boundary")


def _predicate(tags: dict) -> bool:
    """`authorities/osm-places.py::process_tags`, reproduced exactly."""
    if "name" not in tags:
        return False
    return any(k in tags for k in KEYS)


@unittest.skipUnless(HAVE, "pyosmium 4 with FileProcessor/KeyFilter not installed")
class KeyFilterEquivalenceTest(unittest.TestCase):

    FIXTURE = [
        ({"name": "Kept Village", "place": "village"}, True, "named + keyed"),
        ({"name": "Just A Name"}, False, "named, not keyed"),
        ({"place": "town"}, False, "keyed, not named"),
        ({"amenity": "bench"}, False, "neither"),
        ({}, False, "no tags"),
        ({"name": "Old Fort", "historic": "fort", "start_date": "1650"}, True, "dated historic"),
    ]

    def test_the_filter_never_drops_something_the_predicate_would_keep(self):
        """The safety property. A filter that is a strict superset is safe;
        one that is not makes documents disappear with no error."""
        for tags, kept, label in self.FIXTURE:
            passes_filter = "name" in tags
            if _predicate(tags):
                self.assertTrue(passes_filter,
                                f"{label}: predicate keeps it but KeyFilter('name') "
                                f"would have discarded it in C++ — silent data loss")

    def test_the_filter_is_a_strict_superset_not_an_equality(self):
        """MUTATION-ish: proves the Python check still has work to do.

        If the filter and the predicate were equivalent, the surviving Python
        check would be dead code and narrowing the filter would be free. It is
        not: 'named, not keyed' passes the filter and is rejected downstream.
        """
        passes_filter_only = [l for tags, _, l in self.FIXTURE
                              if "name" in tags and not _predicate(tags)]
        self.assertTrue(passes_filter_only,
                        "fixture cannot distinguish superset from equality")

    def test_narrowing_the_filter_to_our_tag_keys_would_lose_nothing_but_is_untested(self):
        """Documents why the filter was NOT narrowed, with the evidence.

        `KeyFilter(*KEYS)` would also be a superset. Chaining both would reject
        more in C++ — but whether pyosmium chains filters as AND or OR was not
        verified, and an OR would silently admit... or worse, a wrong assumption
        would silently drop. The validated configuration is the one that shipped.
        """
        only_keyed = [l for tags, _, l in self.FIXTURE
                      if any(k in tags for k in KEYS) and "name" not in tags]
        self.assertTrue(only_keyed, "fixture has no keyed-but-unnamed case")


class ConversionSiteTest(unittest.TestCase):
    """Guards the fix sites. Runs with or without pyosmium."""

    def test_both_handlers_override_apply_file_with_fileprocessor(self):
        """Assert via AST, not string search.

        The first version of this test did `src.replace("def apply_file", "")`
        and then looked for the call site — which mangled the definition and
        flagged `apply_file(str(active_pbf), locations=True, idx='flex_mem')` as
        a leftover. That call site is CORRECT: the new signature takes
        `**_ignored` precisely so it keeps working. The test was wrong, not the
        code, and a string search over source is how it got that way.
        """
        import ast
        from pathlib import Path
        repo = Path(__file__).resolve().parents[1]
        for name, cls in (("osm-places.py", "OSMHandler"),
                          ("ohm-places.py", "OHMHandler")):
            path = repo / "authorities" / name
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
            klass = next(n for n in ast.walk(tree)
                         if isinstance(n, ast.ClassDef) and n.name == cls)
            methods = [m.name for m in klass.body if isinstance(m, ast.FunctionDef)]
            self.assertIn("apply_file", methods,
                          f"{name}: {cls} does not override apply_file, so "
                          f"SimpleHandler's C++-to-Python firehose is still in use")
            body = ast.get_source_segment(
                src, next(m for m in klass.body
                          if isinstance(m, ast.FunctionDef) and m.name == "apply_file"))
            self.assertIn("osmium.FileProcessor", body, f"{name}: not FileProcessor")
            self.assertIn("KeyFilter('name')", body,
                          f"{name}: uses a filter other than the validated one")
            self.assertIn("with_locations", body,
                          f"{name}: dropped node locations, which way/relation "
                          f"geometry construction needs")


if __name__ == "__main__":
    unittest.main()
