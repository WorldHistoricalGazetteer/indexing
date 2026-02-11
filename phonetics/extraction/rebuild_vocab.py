#!/usr/bin/env python3
"""
Rebuild vocabulary from DuckDB.

Run standalone to fix vocabulary without interrupting a running pipeline.

Usage:
    python -m phonetics.extraction.rebuild_vocab \
        --db-path /ix1/ishi/data/toponyms.db \
        --output-dir /ix1/ishi/models/phonetic/data/v6/vocab
"""

import argparse
import json
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))

from phonetics.utils.script_detection import Script, SCRIPT_RANGES

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def rebuild_vocab_stats_from_toponyms(conn):
    logger.info("Rebuilding vocab stats from toponyms using DuckDB aggregation")

    conn.execute("DELETE FROM observed_chars")
    conn.execute("DELETE FROM script_stats")

    conn.execute("""
        INSERT INTO script_stats (script, count)
        SELECT script, COUNT(*) AS count
        FROM toponyms
        WHERE script IS NOT NULL AND script != ''
        GROUP BY script
    """)

    conn.execute("""
        INSERT INTO observed_chars (char, script, count)
        SELECT ch AS char, script, COUNT(*) AS count
        FROM (
            SELECT UNNEST(string_split(lower(name), '')) AS ch, script
            FROM toponyms
            WHERE name IS NOT NULL AND name != ''
              AND script IS NOT NULL AND script != ''
        ) t
        WHERE ch IS NOT NULL AND ch != '' AND ch != ' '
        GROUP BY ch, script
    """)

    observed_chars = defaultdict(list)
    for ch, script, count in conn.execute(
        "SELECT char, script, count FROM observed_chars"
    ).fetchall():
        observed_chars[ch].append((script, count))

    observed_scripts = {
        row[0] for row in conn.execute("SELECT script FROM script_stats").fetchall()
    }

    logger.info(f"Rebuilt: {len(observed_chars):,} unique chars, {len(observed_scripts)} scripts")
    return dict(observed_chars), observed_scripts


def generate_vocabulary(conn, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)

    # Ensure stats tables exist (may not if DB was built by an older script)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS observed_chars (
            char VARCHAR,
            script VARCHAR,
            count INTEGER DEFAULT 1,
            PRIMARY KEY (char, script)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS script_stats (
            script VARCHAR PRIMARY KEY,
            count INTEGER DEFAULT 0
        )
    ''')

    has_chars = conn.execute("SELECT 1 FROM observed_chars LIMIT 1").fetchone() is not None
    has_scripts = conn.execute("SELECT 1 FROM script_stats LIMIT 1").fetchone() is not None

    if not has_chars or not has_scripts:
        logger.warning("observed_chars/script_stats empty; rebuilding from toponyms table")
        observed_chars, observed_scripts = rebuild_vocab_stats_from_toponyms(conn)
    else:
        observed_chars = defaultdict(list)
        for ch, script, count in conn.execute(
            "SELECT char, script, count FROM observed_chars"
        ).fetchall():
            observed_chars[ch].append((script, count))
        observed_scripts = {
            row[0] for row in conn.execute("SELECT script FROM script_stats").fetchall()
        }

    logger.info(f"Observed {len(observed_chars):,} unique characters across {len(observed_scripts)} scripts")

    vocab = {'<PAD>': 0, '<UNK>': 1, '<SPACE>': 2}
    next_id = 10

    included_scripts = set()
    script_char_counts = defaultdict(int)

    logger.info("Adding ASCII printable range")
    for cp in range(32, 127):
        char = chr(cp)
        if char != ' ' and char not in vocab:
            vocab[char] = next_id
            next_id += 1
            script_char_counts['ASCII'] += 1

    logger.info("Expanding Unicode ranges for observed scripts")
    for script_name in observed_scripts:
        try:
            script = Script(script_name)
        except ValueError:
            logger.warning(f"Unknown script in data: {script_name}")
            continue

        ranges = SCRIPT_RANGES.get(script)
        if not ranges:
            logger.warning(f"No Unicode ranges defined for {script_name}")
            continue

        included_scripts.add(script_name)
        count_before = len(vocab)

        for start, end in ranges:
            for cp in range(start, end + 1):
                try:
                    char = chr(cp)
                    if unicodedata.category(char).startswith('C'):
                        continue
                    if char not in vocab:
                        vocab[char] = next_id
                        next_id += 1
                except (ValueError, OverflowError):
                    continue

        added = len(vocab) - count_before
        script_char_counts[script_name] = added
        logger.info(f"  {script_name}: {added:,} characters")

    for char, char_script_counts in observed_chars.items():
        if char not in vocab and char.strip():
            vocab[char] = next_id
            next_id += 1
            best_script = (
                max(char_script_counts, key=lambda x: x[1])[0]
                if char_script_counts else 'OTHER'
            )
            script_char_counts[best_script] += 1

    vocab_data = {
        'version': 4,
        'char_to_id': vocab,
        'observed_scripts': list(observed_scripts),
        'included_scripts': list(included_scripts),
        'stats': {
            'total_chars': len(vocab),
            'by_script': dict(script_char_counts),
        }
    }

    vocab_path = output_dir / 'char_vocab.json'
    with open(vocab_path, 'w', encoding='utf-8') as f:
        json.dump(vocab_data, f, ensure_ascii=False, indent=2)
    logger.info(f"Vocabulary saved: {vocab_path}")
    logger.info(f"Total vocabulary size: {len(vocab):,}")

    languages = [
        row[0] for row in conn.execute(
            "SELECT DISTINCT lang FROM toponyms WHERE lang IS NOT NULL AND lang != ''"
        ).fetchall()
    ]
    lang_vocab = {'<UNK>': 0}
    for i, lang in enumerate(sorted(languages), start=1):
        lang_vocab[lang] = i
    with open(output_dir / 'lang_vocab.json', 'w', encoding='utf-8') as f:
        json.dump({'version': 1, 'lang_to_id': lang_vocab}, f, indent=2)
    logger.info(f"Language vocabulary: {len(lang_vocab)} entries")

    script_vocab = {s.value: i for i, s in enumerate(Script)}
    with open(output_dir / 'script_vocab.json', 'w') as f:
        json.dump({'version': 1, 'script_to_id': script_vocab}, f, indent=2)
    logger.info(f"Script vocabulary: {len(script_vocab)} entries")

    return vocab_data['stats']


def main():
    parser = argparse.ArgumentParser(description='Rebuild vocabulary from DuckDB')
    parser.add_argument('--db-path', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()

    conn = duckdb.connect(str(args.db_path), read_only=False)
    stats = generate_vocabulary(conn, args.output_dir)
    conn.close()

    logger.info(f"Done. Stats: {json.dumps(stats, indent=2)}")


if __name__ == '__main__':
    main()