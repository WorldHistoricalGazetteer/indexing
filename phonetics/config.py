"""
Configuration for Phonetic Similarity Model.

Defines model architecture, training hyperparameters, and language mappings.
"""


class Config:
    """Model and training configuration."""

    # Vocabulary
    VOCAB_SIZE = 10000
    NUM_LANGS = 300

    # Model dimensions
    CHAR_EMBED_DIM = 64
    LANG_EMBED_DIM = 32
    PHONETIC_FEAT_DIM = 24  # PanPhon feature dimension
    HIDDEN_DIM = 128
    EMBED_DIM = 64
    NUM_LAYERS = 2
    DROPOUT = 0.2

    # Self-Attention configuration (NEW in v2)
    NUM_ATTENTION_HEADS = 2  # 1-2 heads as specified
    ATTENTION_DROPOUT = 0.1

    # Training
    BATCH_SIZE = 256
    SUBSAMPLE_PAIRS = 5000000
    LEARNING_RATE = 1e-3
    PHASE1_EPOCHS = 50
    PHASE2_EPOCHS = 30
    PHASE3_EPOCHS = 20
    TRIPLET_MARGIN = 0.3
    ALIGNMENT_COSINE_WEIGHT = 0.5

    # Curriculum Hard Negatives (NEW in v2)
    # Stage A: Orthographically close, phonetically distant
    STAGE_A_EDIT_DISTANCE_MAX = 3  # anyascii edit distance threshold
    STAGE_A_PHONETIC_DISTANCE_MIN = 0.5  # PanPhon cosine distance threshold

    # Stage B: Model-mined false positives
    STAGE_B_SIMILARITY_THRESHOLD = 0.85  # Conservative threshold for mining

    # Data
    SIMILARITY_THRESHOLD = 0.5
    MAX_TOPONYM_LEN = 50

    # Epitran language mappings (ISO 639-1 → Epitran code)
    EPITRAN_LANGS = {
        'af': 'afr-Latn',
        'am': 'amh-Ethi',
        'ar': 'ara-Arab',
        'az': 'aze-Latn',
        'be': 'bel-Cyrl',
        'bg': 'bul-Cyrl',
        'bn': 'ben-Beng',
        'bs': 'bos-Latn',
        'ca': 'cat-Latn',
        'cs': 'ces-Latn',
        'cy': 'cym-Latn',
        'da': 'dan-Latn',
        'de': 'deu-Latn',
        'el': 'ell-Grek',
        'en': 'eng-Latn',
        'es': 'spa-Latn',
        'et': 'est-Latn',
        'fa': 'fas-Arab',
        'fi': 'fin-Latn',
        'fr': 'fra-Latn',
        'ga': 'gle-Latn',
        'ha': 'hau-Latn',
        'he': 'heb-Hebr',
        'hi': 'hin-Deva',
        'hr': 'hrv-Latn',
        'hu': 'hun-Latn',
        'hy': 'hye-Armn',
        'id': 'ind-Latn',
        'is': 'isl-Latn',
        'it': 'ita-Latn',
        'ja': 'jpn-Hrgn',  # Hiragana only
        'ka': 'kat-Geor',
        'kk': 'kaz-Cyrl',
        'km': 'khm-Khmr',
        'ko': 'kor-Hang',
        'ky': 'kir-Cyrl',
        'la': 'lat-Latn',
        'lt': 'lit-Latn',
        'lv': 'lav-Latn',
        'mk': 'mkd-Cyrl',
        'ml': 'mal-Mlym',
        'mn': 'mon-Cyrl',
        'mr': 'mar-Deva',
        'ms': 'msa-Latn',
        'my': 'mya-Mymr',
        'nl': 'nld-Latn',
        'no': 'nor-Latn',
        'pa': 'pan-Guru',
        'pl': 'pol-Latn',
        'pt': 'por-Latn',
        'ro': 'ron-Latn',
        'ru': 'rus-Cyrl',
        'si': 'sin-Sinh',
        'sk': 'slk-Latn',
        'sl': 'slv-Latn',
        'sq': 'sqi-Latn',
        'sr': 'srp-Cyrl',
        'sv': 'swe-Latn',
        'sw': 'swa-Latn',
        'ta': 'tam-Taml',
        'te': 'tel-Telu',
        'th': 'tha-Thai',
        'tl': 'tgl-Latn',
        'tr': 'tur-Latn',
        'uk': 'ukr-Cyrl',
        'ur': 'urd-Arab',
        'uz': 'uzb-Latn',
        'vi': 'vie-Latn',
        'yo': 'yor-Latn',
        'zh': 'cmn-Hans',  # Simplified Chinese
        'zh-Hans': 'cmn-Hans',
        'zh-Hant': 'cmn-Hans',  # Approximate
        'zh-cn': 'cmn-Hans',
    }
