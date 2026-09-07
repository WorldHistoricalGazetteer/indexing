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

from phonetics.extraction.rebuild_toponyms_index import (
    _embedding_from_packed_features)


def _shipped_select():
    """Return run_index's SQL as shipped, so the tests read the real file."""
    import pathlib
    src = pathlib.Path("phonetics/inference/update_es.py").read_text(encoding="utf-8")
    i = src.index("SELECT t.toponym_id")
    return src[i:src.index(chr(39)*3, i)]


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
        emb = _embedding_from_packed_features(packed)
        if emb:
            doc['panphon_embedding'] = emb
    r = romanize(row[1], row[4])
    if r:
        doc['name_romanized'] = r
    return {k: v for k, v in doc.items() if v is not None}


def fake_romanize(name, script):
    return None if script == "LATIN" else name.lower() + "-rom"


class CarryForwardTest(unittest.TestCase):

    def rows(self):
        # a valid PanPhon blob is N x 24 floats (24 features per IPA segment);
        # the first draft used 3 floats and the real derivation rightly
        # returned None for it, which the count assertion caught
        feats = struct.pack('48f', *([0.5] * 48))   # 2 segments
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

    def test_panphon_embedding_is_always_192_dims(self):
        """⚠ The property ES requires, and the one the first version violated.

        `panphon_features` is N×24 floats — 24 PanPhon features per IPA
        SEGMENT — so its length varies with the name. `panphon_embedding` is
        the fixed 192-d (8 position bins × 24 features) pooling of it.

        Unpacking the blob directly yields 192, 240, 360 … dims. ES sets the
        dense_vector mapping from the FIRST document and rejects every
        differently-sized one thereafter with "Cannot update parameter [dims]".
        Job 11173713 lost 31,757,518 of 73,479,069 documents to exactly that
        and still exited 0, because the bulk helper counts errors without
        raising.

        So the assertion is on INVARIANCE ACROSS INPUT LENGTHS, not on a value:
        a test using one blob length would have passed against the bug.
        """
        for segments in (1, 2, 5, 17, 40):
            packed = struct.pack(f"{segments*24}f", *([0.25] * (segments * 24)))
            row = ("x@en", "X", "en", None, "CJK", "ipa", packed, "gn", "gn:1")
            doc = build_doc(row, "2026-09-07", fake_romanize)
            self.assertEqual(len(doc['panphon_embedding']), 192,
                             f"{segments} segments must still yield 192 dims")

    def test_a_malformed_blob_yields_no_field(self):
        """Not a multiple of 24 floats: absent, never a wrong-length vector."""
        row = ("x@en", "X", "en", None, "CJK", "ipa",
               struct.pack("5f", *([0.1] * 5)), "gn", "gn:1")
        doc = build_doc(row, "2026-09-07", fake_romanize)
        self.assertNotIn('panphon_embedding', doc)

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
        sel = _shipped_select()
        for col in ("t.ipa", "t.panphon_features"):
            self.assertIn(col, sel, f"{col} missing from run_index's SELECT")

    def test_the_blob_is_not_in_the_grouping_key(self):
        """Regression guard for a filesystem incident, not a style preference.

        `panphon_features` is a ~768-byte BLOB. Adding it (and `ipa`) to the
        GROUP BY made every one of 73.5M hash-table entries carry a 768-byte
        key -- tens of GB of hash table, which DuckDB spilled to
        `<dbfile>.tmp`, i.e. BESIDE THE DATABASE on /vast. That drove ~86 GB of
        spill and took /vast from 128 GB to 42 GB, under Elasticsearch's 51 GB
        flood-stage watermark, on job 11173564.

        ANY_VALUE is exact rather than merely cheaper: `toponym_id` is the key
        of `toponyms` and is already in the grouping key, so both columns are
        functionally dependent on the group and there is exactly one value to
        choose from.
        """
        sel = _shipped_select()
        group_by = sel[sel.index("GROUP BY"):]
        self.assertNotIn("panphon_features", group_by,
                         "panphon_features is a BLOB and must NOT be in the "
                         "grouping key -- use ANY_VALUE (see job 11173564)")
        self.assertNotIn("t.ipa", group_by,
                         "ipa must not be in the grouping key -- use ANY_VALUE")
        # The wide row must be PROJECTED, never aggregated: the narrow child
        # tables are aggregated in subqueries and joined in afterwards.
        self.assertIn("FROM toponym_namespaces", sel)
        self.assertIn("FROM toponym_attestations", sel)
        for gb in [seg for seg in sel.split("GROUP BY")[1:]]:
            head = gb.strip().splitlines()[0]
            self.assertNotIn("panphon", head,
                             "no GROUP BY may carry the BLOB: " + head)
            self.assertNotIn("t.name", head,
                             "the wide row must not be grouped at all: " + head)


if __name__ == "__main__":
    unittest.main()
