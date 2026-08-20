"""ukhc counties carry their historic name variants, not just one modern name.

place#204. Every `ukhc` document held exactly one toponym — the canonical
modern-standard county name — so `Somersetshire`, `Yorks` and `Salop` matched
nothing at all in the namespace historians reconcile English county columns
against. Those columns are written overwhelmingly in the forms that failed.

The variants are a GENERATED TABLE, not a rule, and these tests pin the reasons:
`-shire` alternation is unsafe in both directions (there is no Sussexshire; and
stripping the suffix yields the county town, not the county), so the table is
sourced from the Historic Counties Standard, the shapefile's own ABBR field, the
Chapman codes, GeoNames and Wikidata.

Pure-function tests over the committed table + `process_county`; no shapefile,
no network, no Elasticsearch.
"""

import importlib.util
import json
import unittest
from pathlib import Path

_AUTHORITIES = Path(__file__).resolve().parent.parent / "authorities"

# The module name is hyphenated (authorities/ukhc-places.py) — load by path.
_spec = importlib.util.spec_from_file_location(
    "ukhc_places", str(_AUTHORITIES / "ukhc-places.py"))
ukhc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ukhc)

TABLE = json.loads(
    (_AUTHORITIES / "ukhc" / "name_variants.json").read_text(encoding="utf-8"))


def _labels(code: str) -> set[str]:
    return {v["label"].casefold() for v in TABLE["counties"][code]["variants"]}


def _doc(code: str, name: str) -> dict:
    return ukhc.process_county(
        {"NAME": name, "COUNTY": "irrelevant", "HCS_CODE": code},
        {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
    )


def _toponym_labels(doc: dict) -> set[str]:
    return {t["toponym_id"].rpartition("@")[0].casefold() for t in doc["toponyms"]}


class TestTableShape(unittest.TestCase):
    def test_all_ninety_two_counties_are_present(self):
        self.assertEqual(len(TABLE["counties"]), 92)

    def test_every_variant_carries_a_label_lang_and_provenance(self):
        for code, county in TABLE["counties"].items():
            for v in county["variants"]:
                self.assertTrue(v.get("label", "").strip(), code)
                self.assertTrue(v.get("lang", "").strip(), code)
                self.assertIn("source", v, code)

    def test_sources_are_all_declared_in_the_metadata(self):
        declared = set(TABLE["_meta"]["sources"])
        used = {v["source"] for c in TABLE["counties"].values() for v in c["variants"]}
        self.assertEqual(used - declared, set())

    def test_no_county_repeats_a_variant(self):
        for code, county in TABLE["counties"].items():
            labels = [v["label"].casefold() for v in county["variants"]]
            self.assertEqual(len(labels), len(set(labels)), code)

    def test_a_variant_never_repeats_the_canonical_name(self):
        for code, county in TABLE["counties"].items():
            self.assertNotIn(county["name"].casefold(), _labels(code), code)


class TestTheAcceptanceCases(unittest.TestCase):
    """The forms named in place#204's acceptance criteria."""

    def test_shire_alternates(self):
        self.assertIn("somersetshire", _labels("SMS"))
        self.assertIn("devonshire", _labels("DVN"))
        self.assertIn("dorsetshire", _labels("DRS"))

    def test_record_office_contractions(self):
        for code, abbr in (("YRK", "yorks"), ("LCS", "lancs"), ("SHP", "salop"),
                           ("HMP", "hants"), ("BED", "beds"), ("BUC", "bucks"),
                           ("HTF", "herts"), ("NOT", "notts"), ("OXD", "oxon"),
                           ("STF", "staffs"), ("WTS", "wilts"), ("WRC", "worcs"),
                           ("NHP", "northants"), ("MSX", "middx")):
            self.assertIn(abbr, _labels(code), code)

    def test_riding_queries_reach_the_county(self):
        # place#204 option (a): the county-QUALIFIED riding forms only.
        self.assertIn("yorks, west riding", _labels("YRK"))
        self.assertIn("west riding of yorkshire", _labels("YRK"))

    def test_the_bare_riding_name_stays_with_the_riding(self):
        # The ridings are real places elsewhere in WHG (vob_cty / vob_rc), and
        # the Historic Counties Standard says they are NOT historic counties.
        # Yorkshire must not claim the unqualified name.
        self.assertNotIn("west riding", _labels("YRK"))
        self.assertNotIn("east riding", _labels("YRK"))


class TestUnsafeFormsStayOut(unittest.TestCase):
    """The reason the table is generated rather than derived."""

    def test_the_county_town_never_becomes_a_county_toponym(self):
        # Stripping "-shire" yields the town. A nested toponym has no weight, so
        # there is no way to enter these at lower rank; they stay out.
        for code, town in (("YRK", "york"), ("BED", "bedford"), ("LCS", "lancaster"),
                           ("ABN", "aberdeen"), ("OXD", "oxford"), ("CHE", "chester")):
            self.assertNotIn(town, _labels(code), f"{code}/{town}")

    def test_no_fictional_shire_forms(self):
        # Blanket suffixing would invent all of these; no source attests them.
        for code, bogus in (("SUS", "sussexshire"), ("NRF", "norfolkshire"),
                            ("SFF", "suffolkshire"), ("MSX", "middlesexshire"),
                            ("CNW", "cornwallshire"), ("KNT", "kentshire")):
            self.assertNotIn(bogus, _labels(code), f"{code}/{bogus}")

    def test_an_attested_shire_form_is_kept_even_where_the_rule_would_be_wrong(self):
        # The distinction is sourcing, not spelling: a rule would never produce
        # "Angleseyshire" safely, but GeoNames attests it, so it stays. Every
        # variant in the table exists because a source records it.
        entry = next(v for v in TABLE["counties"]["AGL"]["variants"]
                     if v["label"].casefold() == "angleseyshire")
        self.assertEqual(entry["source"], "geonames")

    def test_genuine_county_aliases_survive_the_county_field_omission(self):
        # Dropping COUNTY costs nothing: its non-settlement values are in ABBR,
        # and Brecknock / Merioneth come in from Schedule 1.
        self.assertIn("salop", _labels("SHP"))
        self.assertIn("hants", _labels("HMP"))
        self.assertIn("county of brecknock", _labels("BRN"))
        self.assertIn("county of merioneth", _labels("MRN"))


class TestSourcedVariants(unittest.TestCase):
    def test_welsh_names_come_from_the_standard(self):
        # Schedule 1 marks the official Welsh forms with an asterisk; the marker
        # must be stripped and the language recorded, not left as English.
        welsh = {v["label"] for v in TABLE["counties"]["AGL"]["variants"]
                 if v["lang"] == "cy"}
        self.assertIn("Sir Fôn", welsh)
        self.assertFalse(any(l.endswith("*") for l in _labels("AGL")))

    def test_alias_languages_are_preserved_not_flattened_to_english(self):
        # Wikidata returns aliases in per-language buckets; flattening them
        # tagged every Irish and Welsh form as English.
        langs = {v["label"]: v["lang"] for v in TABLE["counties"]["ANM"]["variants"]
                 if v["source"] == "wikidata"}
        self.assertEqual(langs.get("Aontroma"), "ga")
        self.assertEqual(langs.get("Swydd Antrim"), "cy")
        # (Upstream is not perfect — Wikidata files the Irish "Contae Aontroma"
        # in its Welsh bucket. We record what the source says rather than
        # second-guessing it; the label is right, only its tag is off.)

    def test_both_code_systems_are_indexed_and_distinct(self):
        # The Standard is explicit that HCS codes are NOT Chapman codes.
        sources = {v["source"]: v["label"] for v in TABLE["counties"]["YRK"]["variants"]}
        self.assertEqual(sources.get("hcs-code"), "YRK")
        self.assertEqual(sources.get("chapman-code"), "YKS")

    def test_ross_and_cromarty_share_one_chapman_code(self):
        # A genuine 2:1 mapping — GENUKI has no separate code for either.
        for code in ("RSS", "CRT"):
            chap = [v["label"] for v in TABLE["counties"][code]["variants"]
                    if v["source"] == "chapman-code"]
            self.assertEqual(chap, ["ROC"], code)


class TestDocumentBuilding(unittest.TestCase):
    def test_the_canonical_name_stays_first_and_is_the_title(self):
        doc = _doc("SMS", "Somerset")
        self.assertEqual(doc["title"], "Somerset")
        self.assertEqual(doc["toponyms"][0]["toponym_id"], "Somerset@en")

    def test_variants_reach_the_document(self):
        labels = _toponym_labels(_doc("SMS", "Somerset"))
        self.assertIn("somersetshire", labels)
        self.assertIn("som", labels)

    def test_every_toponym_carries_the_county_timespans(self):
        doc = _doc("YRK", "Yorkshire")
        spans = doc["toponyms"][0]["timespans"]
        self.assertTrue(spans)
        for t in doc["toponyms"]:
            self.assertEqual(t["timespans"], spans)

    def test_welsh_counties_keep_their_1542_start_across_all_names(self):
        doc = _doc("AGL", "Anglesey")
        for t in doc["toponyms"]:
            self.assertEqual(t["timespans"][0]["start"]["in"], 1542)

    def test_concordances_are_closematch_not_sameas(self):
        # The Wikidata item IS the historic county; the GeoNames record is a
        # modern admin unit wearing the name. Neither is an identity claim, and
        # the gateway ships these as co-reference edges.
        doc = _doc("SMS", "Somerset")
        self.assertTrue(doc["links"])
        for link in doc["links"]:
            self.assertEqual(link["type"], "closeMatch")
        idents = {l["identifier"] for l in doc["links"]}
        self.assertTrue(any(i.startswith("wd:") for i in idents))

    def test_a_county_with_no_concordance_emits_no_links_key(self):
        # 18 counties resolved to no unambiguous Wikidata historic-county item.
        # They must simply carry none, not an empty list or a guess.
        codes = [c for c, v in TABLE["counties"].items()
                 if not v.get("wikidata_qid") and not v.get("geonames_place_id")]
        self.assertTrue(codes, "expected at least one unresolved county")
        doc = _doc(codes[0], TABLE["counties"][codes[0]]["name"])
        self.assertNotIn("links", doc)

    def test_an_unknown_code_still_yields_the_canonical_name(self):
        doc = _doc("ZZZ", "Nowhereshire")
        self.assertEqual([t["toponym_id"] for t in doc["toponyms"]], ["Nowhereshire@en"])


if __name__ == "__main__":
    unittest.main()
