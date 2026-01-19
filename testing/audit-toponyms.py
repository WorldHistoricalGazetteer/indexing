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

from processing.settings import ES_HOST
es = Elasticsearch(ES_HOST)

# Official Epitran mapping list
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

# Map ES 'script' values to Epitran's ISO 15924 codes
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


def get_iso3_from_iso1(iso1_code):
    """Converts a 2-letter ISO code to a 3-letter ISO code."""
    try:
        language = pycountry.languages.get(alpha_2=iso1_code.lower())
        return language.alpha_3 if language else None
    except:
        return None


def get_iso_info(code):
    """Validates code and returns (English Name, ISO-3 version)."""
    if code == 'und': return ("Undetermined", "und")
    try:
        lang_obj = None
        if len(code) == 2:
            lang_obj = pycountry.languages.get(alpha_2=code.lower())
        elif len(code) == 3:
            lang_obj = pycountry.languages.get(alpha_3=code.lower())

        if lang_obj:
            return (lang_obj.name, lang_obj.alpha_3)
    except:
        pass
    return (None, None)


def fetch_pairs():
    print(f"Fetching aggregations from {ES_HOST}...")

    query = {
        "size": 0,
        "aggs": {
            "lang_script_pairs": {
                "composite": {
                    "size": 1000,
                    "sources": [
                        {
                            "lang": {
                                "terms": {
                                    "script": "doc['lang'].size() == 0 ? 'und' : doc['lang'].value"
                                }
                            }
                        },
                        {
                            "script": {
                                "terms": {
                                    "field": "script"
                                }
                            }
                        }
                    ]
                }
            }
        }
    }

    pairs = []
    while True:
        res = es.search(index="toponyms", body=query)
        buckets = res['aggregations']['lang_script_pairs']['buckets']

        for b in buckets:
            lang = b['key']['lang']
            script = b['key']['script']
            count = b['doc_count']

            # Filter for 2, 3 chars or 'und'
            if len(lang) in [2, 3] or lang == 'und':
                pairs.append({'lang': lang, 'script': script, 'count': count})

        after_key = res['aggregations']['lang_script_pairs'].get('after_key')
        if not after_key:
            break
        query['aggs']['lang_script_pairs']['composite']['after'] = after_key

    return pairs


def audit_report():
    raw_pairs = fetch_pairs()

    table = PrettyTable()
    table.field_names = ["Lang", "ISO-3", "Script", "Count", "ISO Name", "Epitran Key", "Status"]
    table.align["Lang"] = "l"
    table.align["ISO Name"] = "l"
    table.sortby = "Count"
    table.reversesort = True

    for p in raw_pairs:
        name, iso3 = get_iso_info(p['lang'])

        # 1. Resolve Script to Epitran format
        script_val = p['script'].upper()
        epi_script = SCRIPT_MAP.get(script_val, script_val.capitalize()[:4])

        # 2. Construct and validate Epitran Key
        epitran_key = "None"
        if iso3:
            test_key = f"{iso3}-{epi_script}"
            # Check if this exact pair exists in Epitran's manifest
            if test_key in EPITRAN_SUPPORTED:
                epitran_key = test_key

        # 3. Handle Status
        status = "LEGIT" if name else "ROGUE"
        if name and epitran_key == "None":
            status = "GAP (Needs Charsiu)"

        table.add_row([
            p['lang'],
            iso3 if iso3 else "???",
            p['script'],
            f"{p['count']:,}",
            name if name else "N/A",
            epitran_key,
            status
        ])

    print(table)


if __name__ == "__main__":
    audit_report()