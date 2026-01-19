"""
Check number and validity of all language fields in `toponyms` index

Usage:

srun -p htc --mem=64G --cpus-per-task=4 --pty bash
cd /ix1/whcdh/elastic
python -m testing.audit-toponyms
"""
import pycountry
from elasticsearch import Elasticsearch
from prettytable import PrettyTable
from collections import defaultdict

from processing.settings import ES_HOST
es = Elasticsearch(ES_HOST, request_timeout=60)

# 1. Script-to-Language Defaults for "und" entries
SCRIPT_DEFAULTS = {
    "LATIN": "eng",      # English
    "CJK": "zho",        # Default to Mandarin (Simplified)
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
    "CYRILLIC": "rus",   # Default to Russian (statistically most likely)
    "ARABIC": "ara"      # Literary Arabic
}

# 2. Epitran support table
EPITRAN_SUPPORTED = {
    "aar-Latn": "Afar",
    "afr-Latn": "Afrikanns",
    "aii-Syrc": "Assyrian Neo-Aramaic",
    "amh-Ethi": "Amharic",
    "amh-Ethi-pp": "Amharic (more phonetic)",
    "amh-Ethi-red": "Amharic (reduced)",
    "ara-Arab": "Literary Arabic",
    "ava-Cyrl": "Avaric",
    "aze-Cyrl": "Azerbaijani (Cyrillic)",
    "aze-Latn": "Azerbaijani (Latin)",
    "ben-Beng": "Bengali",
    "ben-Beng-red": "Bengali (reduced)",
    "ben-Beng-east": "Eastern Bengali",
    "bho-Deva": "Bhojpuri",
    "bxk-Latn": "Bukusu",
    "cat-Latn": "Catalan",
    "ceb-Latn": "Cebuano",
    "ces-Latn": "Czech",
    "cjy-Latn": "Jin (Wiktionary)",
    "ckb-Arab": "Sorani",
    "cmn-Hans": "Mandarin (Simplified)*",
    "cmn-Hant": "Mandarin (Traditional)*",
    "cmn-Latn": "Mandarin (Pinyin)*",
    "csb-Latn": "Kashubian",
    "dan-Latn": "Danish",
    "cym-Latn": "Welsh (northern)",
    "deu-Latn": "German",
    "deu-Latn-np": "German†",
    "deu-Latn-nar": "German (more phonetic)",
    "eng-Latn": "English‡",
    "epo-Latn": "Esperanto",
    "est-Latn": "Estonian",
    "fas-Arab": "Farsi (Perso-Arabic)",
    "fin-Latn": "Finnish",
    "fra-Latn": "French",
    "fra-Latn-np": "French†",
    "fra-Latn-p": "French (more phonetic)",
    "fry-Latn": "Frisian",
    "ful-Latn": "Fulah",
    "gan-Latn": "Gan (Wiktionary)",
    "gle-Latn": "Irish",
    "glg-Latn": "Galician",
    "got-Goth": "Gothic",
    "got-Latn": "Gothic (Latin)",
    "hak-Latn": "Hakka (pha̍k-fa-sṳ)",
    "hat-Latn-bab": "Haitian (Latin-Babel)",
    "hau-Latn": "Hausa",
    "hin-Deva": "Hindi",
    "hmn-Latn": "Hmong",
    "hrv-Latn": "Croatian",
    "hsn-Latn": "Xiang (Wiktionary)",
    "hun-Latn": "Hungarian",
    "ile-Latn": "Interlingua",
    "ilo-Latn": "Ilocano",
    "ind-Latn": "Indonesian",
    "ita-Latn": "Italian",
    "jam-Latn": "Jamaican",
    "jav-Latn": "Javanese",
    "jpn-Hira": "Japanese (Hiragana)",
    "jpn-Hira-red": "Japanese (Hiragana, reduced)",
    "jpn-Jpan": "Japanese (Hiragana, Katakana, Kanji)",
    "jpn-Kana": "Japanese (Katakana)",
    "jpn-Kana-red": "Japanese (Katakana, reduced)",
    "kan-Knda": "Kannada",
    "kat-Geor": "Georgian",
    "kaz-Cyrl": "Kazakh (Cyrillic)",
    "kaz-Cyrl-bab": "Kazakh (Cyrillic—Babel)",
    "kaz-Latn": "Kazakh (Latin)",
    "kbd-Cyrl": "Kabardian",
    "kab-Latn": "Kabyle",
    "khm-Khmr": "Khmer",
    "kin-Latn": "Kinyarwanda",
    "kir-Arab": "Kyrgyz (Perso-Arabic)",
    "kir-Cyrl": "Kyrgyz (Cyrillic)",
    "kir-Latn": "Kyrgyz (Latin)",
    "kmr-Latn": "Kurmanji",
    "kmr-Latn-red": "Kurmanji (reduced)",
    "kor-Hang": "Korean",
    "lao-Laoo": "Lao",
    "lao-Laoo-prereform": "Lao (Before spelling reform)",
    "lav-Latn": "Latvian",
    "lez-Cyrl": "Lezgian",
    "lij-Latn": "Ligurian",
    "lit-Latn": "Lithuanian",
    "lsm-Latn": "Saamia",
    "ltc-Latn-bax": "Middle Chinese (Baxter and Sagart 2014)",
    "lug-Latn": "Ganda / Luganda",
    "mal-Mlym": "Malayalam",
    "mar-Deva": "Marathi",
    "mlt-Latn": "Maltese",
    "mon-Cyrl-bab": "Mongolian (Cyrillic)",
    "mri-Latn": "Maori",
    "msa-Latn": "Malay",
    "mya-Mymr": "Burmese",
    "nan-Latn": "Hokkien (pe̍h-oē-jī)",
    "nan-Latn-tl": "Hokkien (Tâi-lô)",
    "nhi-Latn": "Western Sierra Puebla Nahuatl",
    "nld-Latn": "Dutch",
    "nno-Latn": "Norwegian (Nynorsk)",
    "nya-Latn": "Chichewa",
    "ood-Latn-alv": "Tohono O'odham (Alvarez–Hale)",
    "ood-Latn-sax": "Tohono O'odham (Saxton)",
    "ori-Orya": "Odia",
    "orm-Latn": "Oromo",
    "pan-Guru": "Punjabi (Eastern)",
    "pol-Latn": "Polish",
    "por-Latn": "Portuguese",
    "quy-Latn": "Ayacucho Quechua / Quechua Chanka",
    "ron-Latn": "Romanian",
    "run-Latn": "Rundi",
    "rus-Cyrl": "Russian",
    "sag-Latn": "Sango",
    "sin-Sinh": "Sinhala",
    "slv-Latn": "Slovene / Slovenian",
    "sna-Latn": "Shona",
    "som-Latn": "Somali",
    "spa-Latn": "Spanish",
    "spa-Latn-eu": "Spanish (Iberian)",
    "sqi-Latn": "Albanian",
    "sro-Latn": "Sardinian (Campidanese)",
    "sro-Latn": "Sardinian (Campidanese)",
    "srp-Latn": "Serbian (Latin)",
    "srp-Cyrl": "Serbian (Cyrillic)",
    "swa-Latn": "Swahili",
    "swa-Latn-red": "Swahili (reduced)",
    "swe-Latn": "Swedish",
    "tam-Taml": "Tamil",
    "tam-Taml-red": "Tamil (reduced)",
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
    "pbu-Arab": "Pashto (Yousafzai)",
    "vie-Latn": "Vietnamese",
    "wuu-Latn": "Shanghainese Wu (Wiktionary)",
    "xho-Latn": "Xhosa",
    "yor-Latn": "Yoruba",
    "yue-Latn": "Cantonese (Jyutping)",
    "yue-Hant": "Cantonese (Character)",
    "zha-Latn": "Zhuang",
    "zul-Latn": "Zulu",
}

# Reverse the map for lookup
EPITRAN_LOOKUP = {v: k for k, v in EPITRAN_SUPPORTED.items()}

# 3. ES Script -> ISO 15924 Mapping
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


def get_iso_identity(raw_code, script):
    """
    Normalizes the identity.
    Returns: (name, iso3, was_inferred)
    """
    # 1. Determine if we need to default based on script
    code_to_lookup = raw_code
    inferred = False

    is_und = not raw_code or raw_code == 'und' or len(raw_code) not in [2, 3]

    if is_und and script in SCRIPT_DEFAULTS:
        iso3 = SCRIPT_DEFAULTS[script]
        name = iso3.upper() + " (script default)"
        return (name, iso3, True)
    elif is_und:
        return ("Undetermined", "und", False)

    # 2. Perform ISO lookup
    try:
        lang_obj = None
        if len(code_to_lookup) == 2:
            lang_obj = pycountry.languages.get(alpha_2=code_to_lookup.lower())
        elif len(code_to_lookup) == 3:
            lang_obj = pycountry.languages.get(alpha_3=code_to_lookup.lower())

        if lang_obj:
            return (lang_obj.name, lang_obj.alpha_3, inferred)
    except:
        pass

    return ("Undetermined", "und", False)


def fetch_and_aggregate():
    print(f"Aggregating from ES and applying Script Defaults...")
    query = {
        "size": 0,
        "aggs": {
            "pairs": {
                "composite": {
                    "size": 1000,
                    "sources": [
                        {"lang": {"terms": {"field": "lang", "missing_bucket": True}}},
                        {"script": {"terms": {"field": "script"}}}
                    ]
                }
            }
        }
    }

    # Store aggregated totals: {(iso3, script): {'count': int, 'name': str, 'inferred': bool}}
    final_agg = defaultdict(lambda: {'count': 0, 'name': '', 'inferred': False})

    while True:
        res = es.search(index="toponyms", body=query)
        buckets = res['aggregations']['pairs']['buckets']

        for b in buckets:
            raw_lang = b['key']['lang']
            script = b['key']['script']
            count = b['doc_count']

            # THE FIX: Resolve identity BEFORE creating aggregation key
            name, iso3, inferred = get_iso_identity(raw_lang, script)

            key = (iso3, script)
            final_agg[key]['count'] += count
            final_agg[key]['name'] = name
            # If any part of this bucket was inferred, mark it so we can see in status
            if inferred:
                final_agg[key]['inferred'] = True

        after_key = res['aggregations']['pairs'].get('after_key')
        if not after_key: break
        query['aggs']['pairs']['composite']['after'] = after_key

    return final_agg


def audit_report():
    aggregated_data = fetch_and_aggregate()
    rows = []

    for (iso3, script), info in aggregated_data.items():
        script_iso = SCRIPT_MAP.get(script.upper(), script.capitalize()[:4])

        epitran_key = "None"
        if iso3 != "und":
            test_key = f"{iso3}-{script_iso}"
            if test_key in EPITRAN_SUPPORTED:
                epitran_key = test_key
            # Special case for CJK
            elif iso3 == "zho" and script.upper() == "CJK":
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
    table.align["ISO Name"] = "l";
    table.align["Count"] = "r"

    for r in rows:
        table.add_row([r['ISO3'], r['Script'], f"{r['Count']:,}", r['ISO_Name'], r['Epitran_Key'], r['Status']])

    print(table)


if __name__ == "__main__":
    audit_report()