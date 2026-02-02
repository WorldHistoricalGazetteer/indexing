"""
Check number and validity of all language fields in `toponyms` index

Usage:

srun -p htc --mem=64G --cpus-per-task=4 --pty bash
cd /ix1/ishi/elastic
python -m testing.audit-toponyms
"""
import pycountry
from elasticsearch import Elasticsearch
from prettytable import PrettyTable
from collections import defaultdict

from processing.settings import ES_HOST
es = Elasticsearch(ES_HOST, request_timeout=60)

# ----------------------------
# 1. Script-to-Language Defaults for "und" entries
# ----------------------------
SCRIPT_DEFAULTS = {
    "CJK": "zho",        # Mandarin (Simplified)
    "HANGUL": "kor",     # Korean
    "THAI": "tha",       # Thai
    "GREEK": "ell",      # Modern Greek
    "KATAKANA": "jpn",   # Japanese
    "HIRAGANA": "jpn",   # Japanese
    "HEBREW": "heb",     # Hebrew
    "GEORGIAN": "kat",   # Georgian
    "DEVANAGARI": "hin", # Hindi
    "BENGALI": "ben",    # Bengali
    "ARMENIAN": "hye",   # Armenian
    "GUJARATI": "guj",   # Gujarati
    "TAMIL": "tam",      # Tamil
    "KANNADA": "kan",    # Kannada
    "MALAYALAM": "mal",  # Malayalam
    "TELUGU": "tel",     # Telugu
    "CYRILLIC": "rus",   # Russian (default for Cyrillic)
    "ARABIC": "ara"      # Arabic
}

# ----------------------------
# 2. Epitran support table
# ----------------------------
EPITRAN_SUPPORTED = {
    "aar-Latn": "Afar",
    "ace-Latn": "Acehnese (EPITRAN-EXT)",
    "afr-Latn": "Afrikanns",
    "aii-Syrc": "Assyrian Neo-Aramaic",
    "amh-Ethi": "Amharic",
    "amh-Ethi-pp": "Amharic (more phonetic)",
    "amh-Ethi-red": "Amharic (reduced)",
    "ara-Arab": "Literary Arabic",
    "arg-Latn": "Aragonese (EPITRAN-EXT)",
    "arz-Arab": "Egyptian Arabic (EPITRAN-EXT)",
    "ast-Latn": "Asturian (EPITRAN-EXT)",
    "ava-Cyrl": "Avaric",
    "azb-Arab": "South Azerbaijani (EPITRAN-EXT)",
    "aze-Cyrl": "Azerbaijani (Cyrillic)",
    "aze-Latn": "Azerbaijani (Latin)",
    "bak-Cyrl": "Bashkir (EPITRAN-EXT)",
    "bam-Latn": "Bambara (EPITRAN-EXT)",
    "ban-Latn": "Balinese (EPITRAN-EXT)",
    "bar-Latn": "Bavarian (EPITRAN-EXT)",
    "bel-Cyrl": "Belarusian (EPITRAN-EXT)",
    "ben-Beng": "Bengali",
    "ben-Beng-red": "Bengali (reduced)",
    "ben-Beng-east": "Eastern Bengali",
    "bho-Deva": "Bhojpuri",
    "bjn-Latn": "Banjar (EPITRAN-EXT)",
    "bod-Tibt": "Tibetan (EPITRAN-EXT)",
    "bos-Latn": "Bosnian (EPITRAN-EXT)",
    "bpy-Beng": "Bishnupriya (EPITRAN-EXT)",
    "bre-Latn": "Breton (EPITRAN-EXT)",
    "bug-Latn": "Buginese (EPITRAN-EXT)",
    "bul-Cyrl": "Bulgarian (EPITRAN-EXT)",
    "bxk-Latn": "Bukusu",
    "cat-Latn": "Catalan",
    "ceb-Latn": "Cebuano",
    "ces-Latn": "Czech",
    "che-Cyrl": "Chechen (EPITRAN-EXT)",
    "chv-Cyrl": "Chuvash (EPITRAN-EXT)",
    "cjy-Latn": "Jin (Wiktionary)",
    "ckb-Arab": "Sorani",
    "cmn-Hans": "Mandarin (Simplified)*",
    "cmn-Hant": "Mandarin (Traditional)*",
    "cmn-Latn": "Mandarin (Pinyin)*",
    "cos-Latn": "Corsican (EPITRAN-EXT)",
    "crh-Latn": "Crimean Tatar (EPITRAN-EXT)",
    "csb-Latn": "Kashubian",
    "cym-Latn": "Welsh (northern)",
    "dan-Latn": "Danish",
    "deu-Latn": "German",
    "deu-Latn-np": "German†",
    "deu-Latn-nar": "German (more phonetic)",
    "diq-Latn": "Zazaki (EPITRAN-EXT)",
    "ell-Grek": "Greek (EPITRAN-EXT)",
    "eng-Latn": "English‡",
    "epo-Latn": "Esperanto",
    "est-Latn": "Estonian",
    "eus-Latn": "Basque (EPITRAN-EXT)",
    "fao-Latn": "Faroese (EPITRAN-EXT)",
    "fas-Arab": "Farsi (Perso-Arabic)",
    "fij-Latn": "Fijian (EPITRAN-EXT)",
    "fil-Latn": "Filipino (EPITRAN-EXT)",
    "fin-Latn": "Finnish",
    "fra-Latn": "French",
    "fra-Latn-np": "French†",
    "fra-Latn-p": "French (more phonetic)",
    "frc-Latn": "Cajun French (EPITRAN-EXT)",
    "frp-Latn": "Franco-Provençal (EPITRAN-EXT)",
    "frr-Latn": "Northern Frisian (EPITRAN-EXT)",
    "fry-Latn": "Frisian",
    "ful-Latn": "Fulah",
    "fur-Latn": "Friulian (EPITRAN-EXT)",
    "gan-Latn": "Gan (Wiktionary)",
    "gla-Latn": "Scottish Gaelic (EPITRAN-EXT)",
    "gle-Latn": "Irish",
    "glg-Latn": "Galician",
    "glk-Arab": "Gilaki (EPITRAN-EXT)",
    "gor-Latn": "Gorontalo (EPITRAN-EXT)",
    "got-Goth": "Gothic",
    "got-Latn": "Gothic (Latin)",
    "gsw-Latn": "Swiss German (EPITRAN-EXT)",
    "guj-Gujr": "Gujarati (EPITRAN-EXT)",
    "hak-Latn": "Hakka (pha̍k-fa-sṳ)",
    "hat-Latn": "Haitian Creole (EPITRAN-EXT)",
    "hat-Latn-bab": "Haitian (Latin-Babel)",
    "hau-Latn": "Hausa",
    "hbs-Latn": "Serbo-Croatian (EPITRAN-EXT)",
    "hin-Deva": "Hindi",
    "hmn-Latn": "Hmong",
    "hrv-Latn": "Croatian",
    "hsb-Latn": "Upper Sorbian (EPITRAN-EXT)",
    "hsn-Latn": "Xiang (Wiktionary)",
    "hun-Latn": "Hungarian",
    "hye-Armn": "Armenian (EPITRAN-EXT)",
    "hyw-Armn": "Western Armenian (EPITRAN-EXT)",
    "ibo-Latn": "Igbo (EPITRAN-EXT)",
    "ido-Latn": "Ido (EPITRAN-EXT)",
    "ile-Latn": "Interlingua",
    "ilo-Latn": "Ilocano",
    "ina-Latn": "Interlingua (EPITRAN-EXT)",
    "ind-Latn": "Indonesian",
    "isl-Latn": "Icelandic (EPITRAN-EXT)",
    "ita-Latn": "Italian",
    "jam-Latn": "Jamaican",
    "jav-Latn": "Javanese",
    "jpn-Hira": "Japanese (Hiragana)",
    "jpn-Hira-red": "Japanese (Hiragana, reduced)",
    "jpn-Jpan": "Japanese (Hiragana, Katakana, Kanji)",
    "jpn-Kana": "Japanese (Katakana)",
    "jpn-Kana-red": "Japanese (Katakana, reduced)",
    "kal-Latn": "Greenlandic (EPITRAN-EXT)",
    "kan-Knda": "Kannada",
    "kat-Geor": "Georgian (EPITRAN-EXT)",
    "kaz-Arab": "Kazakh (Arabic) (EPITRAN-EXT)",
    "kaz-Cyrl": "Kazakh (Cyrillic)",
    "kaz-Cyrl-bab": "Kazakh (Cyrillic—Babel)",
    "kaz-Latn": "Kazakh (Latin)",
    "kbd-Cyrl": "Kabardian",
    "kab-Latn": "Kabyle",
    "khm-Khmr": "Khmer (EPITRAN-EXT)",
    "khm-Latn": "Khmer (Romanized) (EPITRAN-EXT)",
    "kin-Latn": "Kinyarwanda",
    "kir-Arab": "Kyrgyz (Perso-Arabic)",
    "kir-Cyrl": "Kyrgyz (Cyrillic)",
    "kir-Latn": "Kyrgyz (Latin)",
    "kmr-Latn": "Kurmanji",
    "kmr-Latn-red": "Kurmanji (reduced)",
    "kon-Latn": "Kongo (EPITRAN-EXT)",
    "kor-Hang": "Korean",
    "kor-Hani": "Korean (Hanja) (EPITRAN-EXT)",
    "kur-Arab": "Kurdish (Arabic) (EPITRAN-EXT)",
    "kur-Latn": "Kurdish (Latin) (EPITRAN-EXT)",
    "lao-Laoo": "Lao",
    "lao-Laoo-prereform": "Lao (Before spelling reform)",
    "lao-Latn": "Lao (Romanized) (EPITRAN-EXT)",
    "lat-Latn": "Latin (EPITRAN-EXT)",
    "lav-Latn": "Latvian",
    "lez-Cyrl": "Lezgian",
    "lij-Latn": "Ligurian",
    "lim-Latn": "Limburgish (EPITRAN-EXT)",
    "lit-Latn": "Lithuanian",
    "lld-Latn": "Ladin (EPITRAN-EXT)",
    "lmo-Latn": "Lombard (EPITRAN-EXT)",
    "lsm-Latn": "Saamia",
    "ltc-Latn-bax": "Middle Chinese (Baxter and Sagart 2014)",
    "ltz-Latn": "Luxembourgish (EPITRAN-EXT)",
    "lug-Latn": "Ganda / Luganda",
    "mal-Mlym": "Malayalam",
    "mar-Deva": "Marathi",
    "min-Latn": "Minangkabau (EPITRAN-EXT)",
    "mkd-Cyrl": "Macedonian (EPITRAN-EXT)",
    "mlg-Latn": "Malagasy (EPITRAN-EXT)",
    "mlt-Latn": "Maltese",
    "mon-Cyrl-bab": "Mongolian (Cyrillic)",
    "mri-Latn": "Maori",
    "msa-Latn": "Malay",
    "mya-Latn": "Burmese (Romanized) (EPITRAN-EXT)",
    "mya-Mymr": "Burmese (EPITRAN-EXT)",
    "mzn-Arab": "Mazanderani (EPITRAN-EXT)",
    "nan-Latn": "Hokkien (pe̍h-oē-jī)",
    "nan-Latn-tl": "Hokkien (Tâi-lô)",
    "nap-Latn": "Neapolitan (EPITRAN-EXT)",
    "nds-Latn": "Low German (EPITRAN-EXT)",
    "nep-Deva": "Nepali (EPITRAN-EXT)",
    "new-Deva": "Newari (EPITRAN-EXT)",
    "nhi-Latn": "Western Sierra Puebla Nahuatl",
    "nld-Latn": "Dutch",
    "nno-Latn": "Norwegian (Nynorsk)",
    "nrm-Latn": "Norman (EPITRAN-EXT)",
    "nya-Latn": "Chichewa",
    "oci-Latn": "Occitan (EPITRAN-EXT)",
    "ood-Latn-alv": "Tohono O'odham (Alvarez–Hale)",
    "ood-Latn-sax": "Tohono O'odham (Saxton)",
    "ori-Orya": "Odia",
    "orm-Latn": "Oromo",
    "oss-Cyrl": "Ossetian (EPITRAN-EXT)",
    "pan-Guru": "Punjabi (Eastern) (EPITRAN-EXT)",
    "pap-Latn": "Papiamento (EPITRAN-EXT)",
    "pbu-Arab": "Pashto (Yousafzai)",
    "pcd-Latn": "Picard (EPITRAN-EXT)",
    "pms-Latn": "Piedmontese (EPITRAN-EXT)",
    "pnb-Arab": "Western Punjabi (EPITRAN-EXT)",
    "pol-Latn": "Polish",
    "por-Latn": "Portuguese",
    "prg-Latn": "Prussian (EPITRAN-EXT)",
    "que-Latn": "Quechua (EPITRAN-EXT)",
    "quy-Latn": "Ayacucho Quechua / Quechua Chanka",
    "rgn-Latn": "Romagnol (EPITRAN-EXT)",
    "roh-Latn": "Romansh (EPITRAN-EXT)",
    "ron-Latn": "Romanian",
    "run-Latn": "Rundi",
    "rus-Cyrl": "Russian",
    "sag-Latn": "Sango",
    "sat-Olck": "Santali (EPITRAN-EXT)",
    "scn-Latn": "Sicilian (EPITRAN-EXT)",
    "sco-Latn": "Scots (EPITRAN-EXT)",
    "sgs-Latn": "Samogitian (EPITRAN-EXT)",
    "sin-Sinh": "Sinhala (EPITRAN-EXT)",
    "slk-Latn": "Slovak (EPITRAN-EXT)",
    "slv-Latn": "Slovene / Slovenian",
    "sme-Latn": "Northern Sami (EPITRAN-EXT)",
    "sna-Latn": "Shona",
    "som-Latn": "Somali",
    "spa-Latn": "Spanish",
    "spa-Latn-eu": "Spanish (Iberian)",
    "sqi-Latn": "Albanian",
    "srd-Latn": "Sardinian (EPITRAN-EXT)",
    "sro-Latn": "Sardinian (Campidanese)",
    "srp-Latn": "Serbian (Latin)",
    "srp-Cyrl": "Serbian (Cyrillic)",
    "sun-Latn": "Sundanese (EPITRAN-EXT)",
    "swa-Latn": "Swahili",
    "swa-Latn-red": "Swahili (reduced)",
    "swe-Latn": "Swedish",
    "szl-Latn": "Silesian (EPITRAN-EXT)",
    "tam-Taml": "Tamil",
    "tam-Taml-red": "Tamil (reduced)",
    "tat-Cyrl": "Tatar (EPITRAN-EXT)",
    "tel-Telu": "Telugu",
    "tgk-Cyrl": "Tajik",
    "tgl-Latn": "Tagalog",
    "tgl-Latn-red": "Tagalog (reduced)",
    "tha-Thai": "Thai",
    "tir-Ethi": "Tigrinya",
    "tir-Ethi-pp": "Tigrinya (more phonemic)",
    "tir-Ethi-red": "Tigrinya (reduced)",
    "tok-Latn": "Toki Pona",
    "tpi-Latn": "Tok Pisin",
    "tsn-latn": "Setswana",
    "tuk-Cyrl": "Turkmen (Cyrillic)",
    "tuk-Latn": "Turkmen (Latin)",
    "tur-Latn": "Turkish (Latin)",
    "tur-Latn-bab": "Turkish (Latin—Babel)",
    "tur-Latn-red": "Turkish (reduced)",
    "ukr-Cyrl": "Ukrainian",
    "urd-Arab": "Urdu",
    "uig-Arab": "Uyghur (Perso-Arabic)",
    "uzb-Cyrl": "Uzbek (Cyrillic)",
    "uzb-Latn": "Uzbek (Latin)",
    "vec-Latn": "Venetian (EPITRAN-EXT)",
    "vie-Latn": "Vietnamese",
    "vls-Latn": "West Flemish (EPITRAN-EXT)",
    "vmf-Latn": "Main-Franconian (EPITRAN-EXT)",
    "vol-Latn": "Volapük (EPITRAN-EXT)",
    "war-Latn": "Waray (EPITRAN-EXT)",
    "wln-Latn": "Walloon (EPITRAN-EXT)",
    "wol-Latn": "Wolof (EPITRAN-EXT)",
    "wuu-Latn": "Shanghainese Wu (Wiktionary)",
    "xho-Latn": "Xhosa",
    "yor-Latn": "Yoruba",
    "yue-Latn": "Cantonese (Jyutping)",
    "yue-Hant": "Cantonese (Character)",
    "zha-Latn": "Zhuang",
    "zul-Latn": "Zulu",
}

# Reverse lookup for Epitran keys
EPITRAN_LOOKUP = {v: k for k, v in EPITRAN_SUPPORTED.items()}

# ----------------------------
# 3. ES Script -> ISO 15924 Mapping
# ----------------------------
SCRIPT_MAP = {
    "LATIN": "Latn",
    "CYRILLIC": "Cyrl",
    "ARABIC": "Arab",
    "GREEK": "Grek",
    "HEBREW": "Hebr",
    "ARMENIAN": "Armn",
    "DEVANAGARI": "Deva",
    "BENGALI": "Beng",
    "GEORGIAN": "Kat",
    "GUJARATI": "Gujr",
    "KANNADA": "Knda",
    "MALAYALAM": "Mlym",
    "TAMIL": "Taml",
    "TELUGU": "Telu",
    "THAI": "Thai",
    "ETHIOPIC": "Ethi",
    "HANGUL": "Hang",
    "HIRAGANA": "Hira",
    "KATAKANA": "Kana",
    "SYRIAC": "Syrc",
    "KHMER": "Khmr",
    "LAO": "Laoo",
    "MYANMAR": "Mymr",
    "SINHALA": "Sinh"
}

# Reverse mapping for normalization
REVERSE_SCRIPT_MAP = {v.upper(): k for k, v in SCRIPT_MAP.items()}

# ----------------------------
# 4. Identity resolution
# ----------------------------
def get_iso_identity(raw_code, script):
    """
    Normalize language code and optionally apply script defaults
    Returns: (display_name, iso3, inferred)
    """
    inferred = False

    # Normalize script
    if script in (None, ""):
        script_norm = None
    else:
        script_norm = script.upper()
        script_norm = REVERSE_SCRIPT_MAP.get(script_norm, script_norm)

    # Determine if raw_code is missing / und
    is_und = raw_code in (None, "", "und") or (isinstance(raw_code, str) and len(raw_code) not in (2, 3))

    # Apply script default if appropriate
    if is_und and script_norm in SCRIPT_DEFAULTS:
        iso3 = SCRIPT_DEFAULTS[script_norm]
        return (f"{iso3.upper()} (script default)", iso3, True)

    # Otherwise try pycountry lookup
    if not raw_code:
        return ("Undetermined", "und", False)
    try:
        if len(raw_code) == 2:
            lang = pycountry.languages.get(alpha_2=raw_code.lower())
        else:
            lang = pycountry.languages.get(alpha_3=raw_code.lower())
        if lang:
            return (lang.name, lang.alpha_3, False)
    except:
        pass

    return ("Undetermined", "und", False)

# ----------------------------
# 5. Fetch from Elasticsearch and aggregate
# ----------------------------
def fetch_and_aggregate():
    print(f"Aggregating from ES and applying Script Defaults...")
    query = {
        "size": 0,
        "query": {
            "bool": {
                "filter": [
                    {
                        "terms": {
                            "namespaces": ["gn", "wd", "tgn"]
                        }
                    }
                ]
            }
        },
        "aggs": {
            "pairs": {
                "composite": {
                    "size": 1000,
                    "sources": [
                        {"lang": {"terms": {"field": "lang", "missing_bucket": True}}},
                        {"script": {"terms": {"field": "script", "missing_bucket": True}}}
                    ]
                }
            }
        }
    }

    final_agg = defaultdict(lambda: {'count': 0, 'name': '', 'inferred': False})

    while True:
        res = es.search(index="toponyms", body=query)
        buckets = res['aggregations']['pairs']['buckets']

        for b in buckets:
            raw_lang = b['key']['lang']
            script = b['key']['script']
            count = b['doc_count']

            name, iso3, inferred = get_iso_identity(raw_lang, script)

            key = (iso3, script)
            final_agg[key]['count'] += count
            final_agg[key]['name'] = name
            if inferred:
                final_agg[key]['inferred'] = True

        after_key = res['aggregations']['pairs'].get('after_key')
        if not after_key:
            break
        query['aggs']['pairs']['composite']['after'] = after_key

    return final_agg

# ----------------------------
# 6. Generate audit report
# ----------------------------
def audit_report():
    aggregated_data = fetch_and_aggregate()
    rows = []

    for (iso3, script), info in aggregated_data.items():
        script_norm = SCRIPT_MAP.get((script or "").upper(), script or "None")

        epitran_key = "None"
        if iso3 != "und":
            test_key = f"{iso3}-{script_norm}"

            # Special case: Japanese with CJK script → Hiragana
            if iso3 == "jpn" and script_norm == "CJK":
                test_key = "jpn-Hira"

            # Special case: Map all Norwegian variants (nob|nor|nno) to nno
            if iso3 in ("nob", "nor", "nno") and script_norm == "Latn":
                test_key = "nno-Latn"

            if test_key in EPITRAN_SUPPORTED:
                epitran_key = test_key
            # Special case for Chinese
            elif iso3 == "zho" and script_norm == "CJK":
                epitran_key = "cmn-Hans"

        # Determine Status
        if iso3 == "und":
            status = "NEEDS_DETECTION"
        elif epitran_key != "None":
            status = "EPITRAN (Inferred)" if info['inferred'] else "EPITRAN"
        else:
            status = "GAP (ByT5)"

        rows.append({
            "ISO3": iso3,
            "Script": script,
            "Count": info['count'],
            "ISO_Name": info['name'],
            "Epitran_Key": epitran_key,
            "Status": status
        })

    rows.sort(key=lambda x: (-x['Count'], x['Script'], x['ISO3']))

    table = PrettyTable(["ISO-3", "Script", "Count", "ISO Name", "Epitran Key", "Status"])
    table.align["ISO Name"] = "l"
    table.align["Count"] = "r"

    for r in rows:
        table.add_row([r['ISO3'], r['Script'], f"{r['Count']:,}", r['ISO_Name'], r['Epitran_Key'], r['Status']])

    print(table)

# ----------------------------
# 7. Main
# ----------------------------
if __name__ == "__main__":
    audit_report()