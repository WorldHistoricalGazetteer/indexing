# authorities/generate_phonetic_features.py

"""
Generate IPA transcriptions and PanPhon feature vectors for all toponyms.

This script:
1. Scrolls through all toponyms in Elasticsearch
2. Generates IPA transcription for each toponym name
3. Computes PanPhon feature vector (24-dimensional phonetic features)
4. Updates the toponym documents with ipa and embedding_panphon fields

PanPhon provides articulatory phonetic features for phonemes:
- 24 binary features (e.g., syllabic, consonantal, sonorant, continuant, etc.)
- Enables phonetic similarity search
- Useful for variant matching across languages

Dependencies:
- epitran: For grapheme-to-phoneme conversion (G2P)
- panphon: For phonetic feature extraction
"""

import sys
from collections import defaultdict
from elasticsearch8 import Elasticsearch, helpers

from authorities.settings import ES_HOST, BATCH_SIZE

es = Elasticsearch(ES_HOST)

# Lazy import heavy dependencies
epitran = None
panphon = None


def setup_phonetic_libraries():
    """
    Import and initialize epitran and panphon libraries.
    These are heavy imports so we do them on-demand.
    """
    global epitran, panphon

    try:
        import epitran as epi_module
        import panphon as pp_module
        epitran = epi_module
        panphon = pp_module
        print("✓ Loaded epitran and panphon libraries")
        return True
    except ImportError as e:
        print(f"ERROR: Missing required libraries: {e}")
        print("\nPlease install:")
        print("  pip install epitran panphon --break-system-packages")
        return False


def get_epitran_transliterator(lang_code):
    """
    Get an epitran transliterator for a given language code.

    Epitran supports various language codes like:
    - eng-Latn (English)
    - fra-Latn (French)
    - deu-Latn (German)
    - spa-Latn (Spanish)
    - etc.

    Returns: epitran.Epitran object or None if language not supported
    """
    # Map common ISO 639-1 codes to epitran codes
    lang_map = {
        'en': 'eng-Latn',
        'fr': 'fra-Latn',
        'de': 'deu-Latn',
        'es': 'spa-Latn',
        'it': 'ita-Latn',
        'pt': 'por-Latn',
        'nl': 'nld-Latn',
        'pl': 'pol-Latn',
        'ru': 'rus-Cyrl',
        'ar': 'ara-Arab',
        'zh': 'cmn-Hans',
        'ja': 'jpn-Hira',
        'ko': 'kor-Hang',
        'tr': 'tur-Latn',
        'sv': 'swe-Latn',
        'da': 'dan-Latn',
        'no': 'nor-Latn',
        'fi': 'fin-Latn',
        'cs': 'ces-Latn',
        'el': 'ell-Grek',
        'he': 'heb-Hebr',
        'hi': 'hin-Deva',
        'bn': 'ben-Beng',
        'vi': 'vie-Latn',
        'th': 'tha-Thai',
        'uk': 'ukr-Cyrl',
        'ro': 'ron-Latn',
        'hu': 'hun-Latn',
        'ca': 'cat-Latn',
        'hr': 'hrv-Latn',
        'sk': 'slk-Latn',
        'bg': 'bul-Cyrl',
    }

    epitran_code = lang_map.get(lang_code, lang_code)

    try:
        return epitran.Epitran(epitran_code)
    except Exception as e:
        # Language not supported
        return None


class PhoneticProcessor:
    """
    Manages phonetic processing with caching for performance.
    """

    def __init__(self):
        self.transliterators = {}  # Cache epitran transliterators
        self.ft = panphon.FeatureTable()  # PanPhon feature table

    def get_ipa(self, text, lang_code='en'):
        """
        Convert text to IPA using epitran.

        Args:
            text: Input text (e.g., "London")
            lang_code: ISO 639-1 language code

        Returns: IPA string or None if conversion fails
        """
        if not text:
            return None

        # Get or create transliterator for this language
        if lang_code not in self.transliterators:
            trans = get_epitran_transliterator(lang_code)
            if trans is None:
                # Try English as fallback
                if lang_code != 'en':
                    trans = get_epitran_transliterator('en')
            self.transliterators[lang_code] = trans

        trans = self.transliterators[lang_code]
        if trans is None:
            return None

        try:
            ipa = trans.transliterate(text)
            return ipa if ipa else None
        except Exception as e:
            return None

    def get_panphon_vector(self, ipa_string):
        """
        Compute PanPhon feature vector from IPA string.

        PanPhon represents each phoneme as a 24-dimensional binary vector
        of articulatory features. For a word, we compute the mean vector
        across all phonemes.

        Args:
            ipa_string: IPA transcription

        Returns: List of 24 floats (averaged features) or None
        """
        if not ipa_string:
            return None

        try:
            # Get feature vectors for each segment (phoneme)
            segments = self.ft.word_to_vector_list(ipa_string, numeric=True)

            if not segments:
                return None

            # Compute mean vector across all segments
            # Each segment is a list of 24 features
            num_features = len(segments[0])
            mean_vector = [
                sum(seg[i] for seg in segments) / len(segments)
                for i in range(num_features)
            ]

            return mean_vector

        except Exception as e:
            return None


def scroll_toponyms(index_name, batch_size=1000):
    """
    Scroll through all toponyms in the index.

    Yields: batches of (doc_id, source_dict) tuples
    """
    query = {
        "query": {
            "match_all": {}
        },
        "_source": ["name", "lang", "ipa", "embedding_panphon"],
        "size": batch_size
    }

    print(f"Starting scroll through {index_name}...")

    # Initial scroll
    resp = es.search(index=index_name, body=query, scroll='5m')
    scroll_id = resp['_scroll_id']
    hits = resp['hits']['hits']

    total = resp['hits']['total']['value']
    print(f"Total toponyms to process: {total:,}")

    batch = []
    processed = 0

    while hits:
        for hit in hits:
            batch.append((hit['_id'], hit['_source']))
            processed += 1

            if len(batch) >= batch_size:
                yield batch
                batch = []

        # Get next batch
        resp = es.scroll(scroll_id=scroll_id, scroll='5m')
        scroll_id = resp['_scroll_id']
        hits = resp['hits']['hits']

    # Yield remaining
    if batch:
        yield batch

    # Clear scroll
    try:
        es.clear_scroll(scroll_id=scroll_id)
    except:
        pass

    print(f"Scroll complete. Processed {processed:,} toponyms.")


def process_batch(batch, processor, force_update=False):
    """
    Process a batch of toponyms and return update actions.

    Args:
        batch: List of (doc_id, source_dict) tuples
        processor: PhoneticProcessor instance
        force_update: If True, regenerate even if ipa/embedding exists

    Returns: List of Elasticsearch update actions
    """
    updates = []

    for doc_id, source in batch:
        # Skip if already processed (unless force_update)
        if not force_update:
            if source.get('ipa') and source.get('embedding_panphon'):
                continue

        name = source.get('name')
        lang = source.get('lang', 'en')

        if not name:
            continue

        # Generate IPA
        ipa = processor.get_ipa(name, lang)

        # Generate PanPhon vector
        panphon_vector = None
        if ipa:
            panphon_vector = processor.get_panphon_vector(ipa)

        # Create update if we have new data
        if ipa or panphon_vector:
            update_doc = {}

            if ipa:
                update_doc['ipa'] = ipa

            if panphon_vector:
                update_doc['embedding_panphon'] = panphon_vector

            updates.append({
                '_op_type': 'update',
                '_index': 'toponyms',
                '_id': doc_id,
                'doc': update_doc
            })

    return updates


def generate_phonetic_features(index_name='toponyms', force_update=False, scroll_batch_size=1000):
    """
    Main function to generate phonetic features for all toponyms.

    Args:
        index_name: Name of toponyms index
        force_update: If True, regenerate even for documents that already have features
        scroll_batch_size: Number of documents to fetch per scroll request
    """
    print("=" * 80)
    print("PHONETIC FEATURE GENERATION")
    print("=" * 80)

    # Setup libraries
    if not setup_phonetic_libraries():
        return

    processor = PhoneticProcessor()

    print(f"\nProcessing index: {index_name}")
    print(f"Force update: {force_update}")
    print(f"Scroll batch size: {scroll_batch_size}")
    print(f"Bulk update batch size: {BATCH_SIZE}")
    print()

    # Track statistics
    stats = {
        'processed': 0,
        'updated': 0,
        'skipped': 0,
        'ipa_generated': 0,
        'panphon_generated': 0,
        'errors': 0
    }

    # Statistics by language
    lang_stats = defaultdict(lambda: {'total': 0, 'ipa_success': 0, 'panphon_success': 0})

    # Scroll through all toponyms
    for batch in scroll_toponyms(index_name, scroll_batch_size):
        # Track language stats for this batch
        for doc_id, source in batch:
            lang = source.get('lang', 'unknown')
            lang_stats[lang]['total'] += 1

        # Process batch
        updates = process_batch(batch, processor, force_update)

        stats['processed'] += len(batch)
        stats['skipped'] += len(batch) - len(updates)

        # Count generated features
        for update in updates:
            doc = update['doc']
            if 'ipa' in doc:
                stats['ipa_generated'] += 1
            if 'embedding_panphon' in doc:
                stats['panphon_generated'] += 1

        # Bulk update
        if updates:
            try:
                success, failed = helpers.bulk(es, updates, raise_on_error=False, stats_only=True)
                stats['updated'] += success
                stats['errors'] += failed

                # Update language stats
                for update in updates:
                    # We'd need to track language per update, skip for now
                    pass

            except Exception as e:
                print(f"Error in bulk update: {e}")
                stats['errors'] += len(updates)

        # Progress report
        if stats['processed'] % 50000 == 0:
            print(f"Progress: {stats['processed']:,} processed, "
                  f"{stats['updated']:,} updated, "
                  f"{stats['skipped']:,} skipped")

    # Final report
    print("\n" + "=" * 80)
    print("PROCESSING COMPLETE")
    print("=" * 80)
    print(f"Total processed:      {stats['processed']:,}")
    print(f"Total updated:        {stats['updated']:,}")
    print(f"Total skipped:        {stats['skipped']:,}")
    print(f"IPA generated:        {stats['ipa_generated']:,}")
    print(f"PanPhon generated:    {stats['panphon_generated']:,}")
    print(f"Errors:               {stats['errors']:,}")

    print("\n" + "-" * 80)
    print("LANGUAGE STATISTICS")
    print("-" * 80)
    print(f"{'Language':<10} {'Total':>10} {'IPA Success':>15} {'PanPhon Success':>18}")
    print("-" * 80)

    for lang, stats in sorted(lang_stats.items(), key=lambda x: x[1]['total'], reverse=True)[:20]:
        print(f"{lang:<10} {stats['total']:>10,} "
              f"{stats['ipa_success']:>15,} "
              f"{stats['panphon_success']:>18,}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description='Generate IPA and PanPhon phonetic features for toponyms'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Force regeneration even if features already exist'
    )
    parser.add_argument(
        '--index',
        default='toponyms',
        help='Name of the toponyms index (default: toponyms)'
    )
    parser.add_argument(
        '--scroll-batch',
        type=int,
        default=1000,
        help='Number of documents per scroll batch (default: 1000)'
    )

    args = parser.parse_args()

    generate_phonetic_features(
        index_name=args.index,
        force_update=args.force,
        scroll_batch_size=args.scroll_batch
    )