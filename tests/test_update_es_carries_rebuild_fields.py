"""`update_es index` must not destroy the fields the rebuild computed.

THE DEFECT THIS PINS. `run_index` DELETES and recreates the toponyms index,
then rebuilds every document from a fixed field list. Three fields the rebuild
had computed at real cost were not in that list — `ipa`, `panphon_embedding`
and `name_romanized` — so each full pipeline run produced them and then threw
them away. Production carried 0 of each against 73,703,777 documents.

⚠ The consequence was invisible. `es_knn_helper` runs its KNN over
`panphon_embedding`; with the field absent it matched nothing, so
`find_similar_in_place` returned `[]` for EVERY place and training-pair
selection was inert — without erroring, because `mget` answers `found: true`
for a document that merely lacks a field.

⚠ WHY THIS TEST COUNTS RATHER THAN SAMPLES. A partial carry-forward — one
branch restoring the field, another not — presents exactly like success: the
mapping has the field, a sampled document shows a value, `exists` is non-zero.
So the assertions compare the number of docs carrying each field against the
number of DuckDB rows that supply it. Presence is not the property; parity is.
"""
import struct
import unittest


def build_doc(row, indexed_at, romanize):
    """Mirror of run_index's per-row document construction.

    Kept as a small pure function so the field set can be tested without an
    Elasticsearch or a 39 GB DuckDB. The row layout is run_index's SELECT:
    (toponym_id, name, lang, lang_variant, script, ipa, panphon_features,
     namespaces, attestations).
    """
    namespaces = row[7].split(',') if row[7] else []
    doc = {
        'name': row[1],
        'lang': row[2] or None,
        'lang_variant': row[3] or None,
        'script': row[4],
        'namespaces': namespaces,
        'primary_namespace': namespaces[0] if namespaces else None,
        'attestations': row[8].split(',') if row[8] else [],
        'indexed_at': indexed_at,
    }
    if row[5]:
        doc['ipa'] = row[5]
    packed = row[6]
    if packed:
        n = len(packed) // 4
        doc['panphon_embedding'] = list(struct.unpack(f'{n}f', packed))
    r = romanize(row[1], row[4])
    if r:
        doc['name_romanized'] = r
    return {k: v for k, v in doc.items() if v is not None}


def fake_romanize(name, script):
    return None if script == "LATIN" else name.lower() + "-rom"


class CarryForwardTest(unittest.TestCase):

    def rows(self):
        feats = struct.pack('3f', 0.0, 0.5, 1.0)
        return [
            # id, name, lang, variant, script, ipa, panphon, ns, attest
            ("北京@zh", "北京", "zh", None, "CJK", "peɪtɕiŋ", feats, "gn", "gn:1"),
            ("London@en", "London", "en", None, "LATIN", "lʌndən", feats, "gn", "gn:2"),
            ("Shanghai@zh", "Shanghai", "zh", "Latn", "LATIN", None, None, "tgn", "tgn:3"),
        ]

    def test_field_counts_match_the_rows_that_supply_them(self):
        docs = [build_doc(r, "2026-09-07", fake_romanize) for r in self.rows()]

        rows = self.rows()
        expect_ipa = sum(1 for r in rows if r[5])
        expect_pan = sum(1 for r in rows if r[6])
        expect_rom = sum(1 for r in rows if fake_romanize(r[1], r[4]))

        got_ipa = sum(1 for d in docs if 'ipa' in d)
        got_pan = sum(1 for d in docs if 'panphon_embedding' in d)
        got_rom = sum(1 for d in docs if 'name_romanized' in d)

        self.assertEqual((got_ipa, got_pan, got_rom),
                         (expect_ipa, expect_pan, expect_rom),
                         "field counts must equal the rows that supply them — "
                         "a partial carry-forward passes a presence check")

    def test_panphon_round_trips_to_the_right_values(self):
        doc = build_doc(self.rows()[0], "2026-09-07", fake_romanize)
        self.assertEqual(doc['panphon_embedding'], [0.0, 0.5, 1.0])

    def test_a_row_with_no_ipa_yields_no_ipa_field(self):
        """The absent case must stay absent, not become an empty string."""
        doc = build_doc(self.rows()[2], "2026-09-07", fake_romanize)
        self.assertNotIn('ipa', doc)
        self.assertNotIn('panphon_embedding', doc)

    def test_latin_names_get_no_romanisation(self):
        doc = build_doc(self.rows()[1], "2026-09-07", fake_romanize)
        self.assertNotIn('name_romanized', doc)

    def test_the_shipped_select_actually_selects_the_columns(self):
        """Guards the other half: the doc code is useless if the SELECT drops them."""
        import pathlib
        src = pathlib.Path("phonetics/inference/update_es.py").read_text(encoding="utf-8")
        i = src.index("SELECT t.toponym_id")
        sel = src[i:src.index("''')", i)]
        for col in ("t.ipa", "t.panphon_features"):
            self.assertIn(col, sel, f"{col} missing from run_index's SELECT")
        self.assertIn("t.ipa", sel[sel.index("GROUP BY"):],
                      "ipa must be in GROUP BY or the query errors")


if __name__ == "__main__":
    unittest.main()
