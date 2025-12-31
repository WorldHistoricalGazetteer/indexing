# authorities/generate_bilstm_embeddings.py

"""
Generate BiLSTM phonetic embeddings for all toponyms.

This script uses a pre-trained BiLSTM model to generate dense phonetic
embeddings from toponym names. Unlike PanPhon which uses hand-crafted
features, BiLSTM learns representations from data.

The BiLSTM model:
- Processes character sequences
- Learns phonetic patterns from training data
- Produces 128-dimensional dense vectors
- Better captures pronunciation patterns than rule-based approaches

This is particularly useful for:
- Names in languages without good G2P (grapheme-to-phoneme) models
- Historical spellings and variants
- Cross-linguistic name matching

Note: This requires a pre-trained model. If you don't have one, this script
will provide a placeholder architecture that you can train or we can use
an existing model like celui-ci or similar.
"""

import sys
import torch
import torch.nn as nn
from collections import defaultdict
from elasticsearch import Elasticsearch, helpers

from authorities.settings import ES_HOST, BATCH_SIZE

es = Elasticsearch(ES_HOST)


class CharBiLSTM(nn.Module):
    """
    Character-level BiLSTM for phonetic embeddings.

    Architecture:
    1. Character embedding layer
    2. Bidirectional LSTM
    3. Mean pooling over sequence
    4. Output: fixed-size vector
    """

    def __init__(
            self,
            vocab_size=128,  # ASCII + extended
            embed_dim=64,
            hidden_dim=64,
            output_dim=128,
            num_layers=2
    ):
        super(CharBiLSTM, self).__init__()

        self.embedding = nn.Embedding(vocab_size, embed_dim, padding_idx=0)
        self.lstm = nn.LSTM(
            embed_dim,
            hidden_dim,
            num_layers=num_layers,
            bidirectional=True,
            batch_first=True
        )
        self.fc = nn.Linear(hidden_dim * 2, output_dim)

    def forward(self, x):
        """
        Args:
            x: (batch_size, seq_len) - character indices

        Returns:
            (batch_size, output_dim) - phonetic embeddings
        """
        # Embed characters
        embedded = self.embedding(x)  # (batch, seq_len, embed_dim)

        # BiLSTM
        lstm_out, _ = self.lstm(embedded)  # (batch, seq_len, hidden_dim*2)

        # Mean pooling over sequence
        pooled = torch.mean(lstm_out, dim=1)  # (batch, hidden_dim*2)

        # Project to output dimension
        output = self.fc(pooled)  # (batch, output_dim)

        return output


class PhoneticEmbedder:
    """
    Generates phonetic embeddings using BiLSTM model.
    """

    def __init__(self, model_path=None, device='cpu'):
        """
        Args:
            model_path: Path to pre-trained model weights (.pt file)
            device: 'cpu' or 'cuda'
        """
        self.device = torch.device(device)
        self.model = CharBiLSTM().to(self.device)
        self.model.eval()

        # Build character vocabulary
        self.char_to_idx = self._build_vocab()
        self.max_len = 50  # Maximum name length

        # Load pre-trained weights if available
        if model_path:
            try:
                self.model.load_state_dict(torch.load(model_path, map_location=self.device))
                print(f"✓ Loaded pre-trained model from {model_path}")
            except Exception as e:
                print(f"Warning: Could not load model from {model_path}: {e}")
                print("Using randomly initialized model (not recommended for production)")

    def _build_vocab(self):
        """
        Build character vocabulary.
        Covers ASCII + common extended characters.
        """
        vocab = {chr(i): i for i in range(32, 127)}  # Printable ASCII

        # Add common extended Latin characters
        extended = "àáâãäåæçèéêëìíîïðñòóôõöøùúûüýþÿ" \
                   "ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖØÙÚÛÜÝÞ" \
                   "āăąćĉċčďđēĕėęěĝğġģĥħĩīĭįıĵķĺļľŀł" \
                   "ńņňŉŋōŏőœŕŗřśŝşšţťŧũūŭůűųŵŷźżž" \
                   "αβγδεζηθικλμνξοπρστυφχψω" \
                   "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ"

        idx = 127
        for char in extended:
            if char not in vocab:
                vocab[char] = idx
                idx += 1

        return vocab

    def text_to_indices(self, text):
        """
        Convert text to character indices.

        Args:
            text: Input string

        Returns: List of character indices
        """
        indices = []
        for char in text[:self.max_len]:
            idx = self.char_to_idx.get(char, self.char_to_idx.get(char.lower(), 1))
            indices.append(idx)

        # Pad to max_len
        while len(indices) < self.max_len:
            indices.append(0)

        return indices

    @torch.no_grad()
    def embed(self, text):
        """
        Generate embedding for a single text.

        Args:
            text: Input string (place name)

        Returns: List of 128 floats (embedding vector)
        """
        if not text:
            return None

        # Convert to indices
        indices = self.text_to_indices(text)

        # Create tensor
        x = torch.tensor([indices], dtype=torch.long, device=self.device)

        # Generate embedding
        embedding = self.model(x)

        # Convert to list
        return embedding.cpu().numpy()[0].tolist()

    @torch.no_grad()
    def embed_batch(self, texts):
        """
        Generate embeddings for multiple texts.

        Args:
            texts: List of strings

        Returns: List of embedding vectors
        """
        if not texts:
            return []

        # Convert to indices
        indices_list = [self.text_to_indices(text) for text in texts]

        # Create tensor
        x = torch.tensor(indices_list, dtype=torch.long, device=self.device)

        # Generate embeddings
        embeddings = self.model(x)

        # Convert to lists
        return embeddings.cpu().numpy().tolist()


def scroll_toponyms(index_name, batch_size=1000):
    """
    Scroll through all toponyms without BiLSTM embeddings.
    """
    query = {
        "query": {
            "bool": {
                "must_not": {
                    "exists": {
                        "field": "embedding_bilstm"
                    }
                }
            }
        },
        "_source": ["name", "lang"],
        "size": batch_size
    }

    print(f"Starting scroll through {index_name}...")
    print("Fetching toponyms without BiLSTM embeddings...")

    resp = es.search(index=index_name, body=query, scroll='5m')
    scroll_id = resp['_scroll_id']
    hits = resp['hits']['hits']

    total = resp['hits']['total']['value']
    print(f"Found {total:,} toponyms to process")

    batch = []
    processed = 0

    while hits:
        for hit in hits:
            batch.append((hit['_id'], hit['_source']))
            processed += 1

            if len(batch) >= batch_size:
                yield batch
                batch = []

        resp = es.scroll(scroll_id=scroll_id, scroll='5m')
        scroll_id = resp['_scroll_id']
        hits = resp['hits']['hits']

    if batch:
        yield batch

    try:
        es.clear_scroll(scroll_id=scroll_id)
    except:
        pass


def process_batch(batch, embedder):
    """
    Process a batch of toponyms and generate embeddings.

    Returns: List of Elasticsearch update actions
    """
    # Extract names
    names = [source.get('name', '') for doc_id, source in batch]

    # Generate embeddings in batch
    embeddings = embedder.embed_batch(names)

    # Create updates
    updates = []
    for (doc_id, source), embedding in zip(batch, embeddings):
        if embedding:
            updates.append({
                '_op_type': 'update',
                '_index': 'toponyms',
                '_id': doc_id,
                'doc': {
                    'embedding_bilstm': embedding
                }
            })

    return updates


def generate_bilstm_embeddings(
        index_name='toponyms',
        model_path=None,
        device='cpu',
        scroll_batch_size=1000
):
    """
    Generate BiLSTM embeddings for all toponyms.

    Args:
        index_name: Name of toponyms index
        model_path: Path to pre-trained model (optional)
        device: 'cpu' or 'cuda'
        scroll_batch_size: Documents per scroll batch
    """
    print("=" * 80)
    print("BiLSTM PHONETIC EMBEDDING GENERATION")
    print("=" * 80)

    # Check for CUDA
    if device == 'cuda' and not torch.cuda.is_available():
        print("CUDA requested but not available, falling back to CPU")
        device = 'cpu'

    print(f"\nDevice: {device}")
    print(f"Index: {index_name}")
    print(f"Model: {model_path if model_path else 'Random initialization (not recommended!)'}")
    print()

    # Initialize embedder
    embedder = PhoneticEmbedder(model_path=model_path, device=device)

    # Track statistics
    stats = {
        'processed': 0,
        'updated': 0,
        'errors': 0
    }

    # Process in batches
    for batch in scroll_toponyms(index_name, scroll_batch_size):
        updates = process_batch(batch, embedder)

        stats['processed'] += len(batch)

        # Bulk update
        if updates:
            try:
                success, failed = helpers.bulk(es, updates, raise_on_error=False, stats_only=True)
                stats['updated'] += success
                stats['errors'] += failed
            except Exception as e:
                print(f"Error in bulk update: {e}")
                stats['errors'] += len(updates)

        # Progress
        if stats['processed'] % 50000 == 0:
            print(f"Processed: {stats['processed']:,}, Updated: {stats['updated']:,}")

    # Final report
    print("\n" + "=" * 80)
    print("PROCESSING COMPLETE")
    print("=" * 80)
    print(f"Total processed:  {stats['processed']:,}")
    print(f"Total updated:    {stats['updated']:,}")
    print(f"Errors:           {stats['errors']:,}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description='Generate BiLSTM phonetic embeddings for toponyms'
    )
    parser.add_argument(
        '--model',
        help='Path to pre-trained BiLSTM model (.pt file)'
    )
    parser.add_argument(
        '--device',
        choices=['cpu', 'cuda'],
        default='cpu',
        help='Device to use (default: cpu)'
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

    if not args.model:
        print("\nWARNING: No pre-trained model specified!")
        print("The script will use random initialization, which will NOT produce meaningful embeddings.")
        print("For production use, you should:")
        print("  1. Train a model on place name data, or")
        print("  2. Use a pre-trained character-level model")
        print()
        response = input("Continue with random initialization? (y/n): ")
        if response.lower() != 'y':
            print("Cancelled.")
            sys.exit(0)

    generate_bilstm_embeddings(
        index_name=args.index,
        model_path=args.model,
        device=args.device,
        scroll_batch_size=args.scroll_batch
    )