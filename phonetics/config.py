"""
Configuration for Phonetic Similarity Model.

Defines model architecture, training hyperparameters, and language mappings.

Note: Vocabulary sizes (VOCAB_SIZE, NUM_LANGS) are conservative defaults.
Actual sizes are determined by the extraction process and loaded from
the vocab files at runtime. The two-pass extraction from gn/wd/tgn
typically yields ~4000 char tokens and ~1000 languages.
"""


class Config:
    """Model and training configuration."""

    # Vocabulary (conservative defaults; actual from vocab files)
    VOCAB_SIZE = 5000   # Actual determined by extraction
    NUM_SCRIPTS = 25    # 20 defined + buffer
    NUM_LANGS = 1200    # Wikidata has many languages

    # Model dimensions
    CHAR_EMBED_DIM = 64
    SCRIPT_EMBED_DIM = 16
    LANG_EMBED_DIM = 16
    PHONETIC_FEAT_DIM = 24  # PanPhon feature dimension
    HIDDEN_DIM = 128
    EMBED_DIM = 128  # Output embedding dimension
    NUM_LAYERS = 2
    DROPOUT = 0.2
    LANG_DROPOUT = 0.5  # Language dropout for training

    # Self-Attention configuration
    NUM_ATTENTION_HEADS = 2
    ATTENTION_DROPOUT = 0.1

    # Training
    BATCH_SIZE = 128
    LEARNING_RATE = 1e-3
    WEIGHT_DECAY = 1e-5
    PHASE1_EPOCHS = 50
    PHASE2_EPOCHS = 50
    PHASE3_EPOCHS = 30
    TRIPLET_MARGIN = 0.3
    MSE_WEIGHT = 1.0
    COSINE_WEIGHT = 1.0
    NOISE_PROB = 0.3

    # Curriculum Hard Negatives (Phase 3)
    # Stage A: Orthographically close, phonetically distant
    STAGE_A_EDIT_DISTANCE_MAX = 3  # anyascii edit distance threshold
    STAGE_A_PHONETIC_DISTANCE_MIN = 0.5  # PanPhon cosine distance threshold

    # Stage B: Model-mined false positives
    STAGE_B_SIMILARITY_THRESHOLD = 0.85  # Conservative threshold for mining

    # Data
    PHONETIC_SIMILARITY_THRESHOLD = 0.35  # Minimum for positive pairs
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
