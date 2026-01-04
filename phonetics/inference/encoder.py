# phonetics/inference/encoder.py
"""
Inference module for phonetic embeddings.

This module provides:
- ToponymEncoder: High-level interface for encoding toponyms to embeddings
- Batch encoding utilities for processing large datasets
- ES index update functionality for populating embeddings

Usage:
    # Single toponym encoding
    from phonetics.inference import ToponymEncoder

    encoder = ToponymEncoder.from_checkpoint(
        checkpoint_path='checkpoints/final_model.pt',
        vocab_dir='data/v2/vocab',
        device='cuda'
    )

    embedding = encoder.encode("London", lang="en")
    embeddings = encoder.encode_batch(["London", "Paris", "Москва"])

    # Update ES index with embeddings
    python -m phonetics.inference.update_es \
        --checkpoint checkpoints/final_model.pt \
        --vocab-dir data/v2/vocab \
        --es-host localhost:9200 \
        --batch-size 1000
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import torch
import torch.nn.functional as F

from phonetics.models.models_v2 import UniversalEncoder, create_student, load_checkpoint
from phonetics.vocab.char_vocab import CharacterVocabulary, ScriptVocabulary, LanguageVocabulary
from phonetics.utils.script_detection import Script, detect_script

logger = logging.getLogger(__name__)


class ToponymEncoder:
    """
    High-level interface for encoding toponyms to phonetic embeddings.

    Handles:
    - Automatic script detection
    - Character vocabulary encoding
    - Batch processing with padding
    - GPU/CPU inference

    Example:
        encoder = ToponymEncoder.from_checkpoint(
            'checkpoints/final_model.pt',
            'data/v2/vocab'
        )

        # Single encoding
        emb = encoder.encode("Constantinople")

        # Batch encoding
        embs = encoder.encode_batch([
            ("London", "en"),
            ("Москва", "ru"),
            ("東京", "ja"),
        ])

        # Search for similar toponyms
        query_emb = encoder.encode("Londinium")
        similarities = encoder.similarity(query_emb, candidate_embs)
    """

    def __init__(
            self,
            model: UniversalEncoder,
            char_vocab: CharacterVocabulary,
            script_vocab: ScriptVocabulary,
            lang_vocab: LanguageVocabulary,
            device: str = 'cpu',
    ):
        self.model = model.to(device)
        self.model.eval()
        self.char_vocab = char_vocab
        self.script_vocab = script_vocab
        self.lang_vocab = lang_vocab
        self.device = device

        # Freeze model
        for param in self.model.parameters():
            param.requires_grad = False

    @classmethod
    def from_checkpoint(
            cls,
            checkpoint_path: str,
            vocab_dir: str,
            device: str = 'cpu',
    ) -> 'ToponymEncoder':
        """
        Load encoder from checkpoint and vocabulary files.

        Args:
            checkpoint_path: Path to model checkpoint (.pt file)
            vocab_dir: Directory containing vocab JSON files
            device: Device to load model to ('cpu' or 'cuda')

        Returns:
            Initialized ToponymEncoder
        """
        vocab_dir = Path(vocab_dir)

        # Load vocabularies
        char_vocab = CharacterVocabulary.load(
            vocab_dir / 'char_vocab.json',
            allow_growth=False
        )
        script_vocab = ScriptVocabulary.load(vocab_dir / 'script_vocab.json')
        lang_vocab = LanguageVocabulary.load(vocab_dir / 'lang_vocab.json')

        logger.info(f"Loaded vocabularies: chars={len(char_vocab)}, "
                    f"scripts={len(script_vocab)}, langs={len(lang_vocab)}")

        # Load checkpoint to get config
        checkpoint = torch.load(checkpoint_path, map_location=device)
        config = checkpoint.get('config', {})

        # Create model with same config
        model = create_student(
            vocab_size=len(char_vocab),
            num_scripts=len(script_vocab),
            num_langs=len(lang_vocab),
            embed_dim=config.get('embed_dim', 128),
            hidden_dim=config.get('hidden_dim', 128),
            num_layers=config.get('num_layers', 2),
            dropout=config.get('dropout', 0.2),
            lang_dropout=0.0,  # No dropout at inference
        )

        # Load weights
        model.load_state_dict(checkpoint['model_state_dict'])
        logger.info(f"Loaded model from {checkpoint_path}")

        return cls(model, char_vocab, script_vocab, lang_vocab, device)

    def _prepare_input(
            self,
            name: str,
            lang: Optional[str] = None,
            script: Optional[Script] = None,
    ) -> Tuple[List[int], int, int]:
        """
        Prepare a single toponym for encoding.

        Returns:
            Tuple of (char_ids, script_id, lang_id)
        """
        # Detect script if not provided
        if script is None:
            script, _ = detect_script(name)

        # Encode characters
        char_ids = self.char_vocab.encode(name, script)

        # Get script and language IDs
        script_id = self.script_vocab.encode(script)
        lang_id = self.lang_vocab.encode(lang)

        return char_ids, script_id, lang_id

    def encode(
            self,
            name: str,
            lang: Optional[str] = None,
            script: Optional[Script] = None,
    ) -> torch.Tensor:
        """
        Encode a single toponym to embedding.

        Args:
            name: Toponym string
            lang: Optional language code (e.g., 'en', 'ru')
            script: Optional script type (auto-detected if not provided)

        Returns:
            Embedding tensor of shape [embed_dim]
        """
        char_ids, script_id, lang_id = self._prepare_input(name, lang, script)

        # Create tensors
        char_ids_t = torch.tensor([char_ids], dtype=torch.long, device=self.device)
        script_ids_t = torch.tensor([script_id], dtype=torch.long, device=self.device)
        lang_ids_t = torch.tensor([lang_id], dtype=torch.long, device=self.device)
        lengths_t = torch.tensor([len(char_ids)], dtype=torch.long)

        # Forward pass
        with torch.no_grad():
            embedding = self.model(char_ids_t, script_ids_t, lang_ids_t, lengths_t)

        return embedding.squeeze(0)

    def encode_batch(
            self,
            toponyms: List[Union[str, Tuple[str, str]]],
            batch_size: int = 256,
            show_progress: bool = False,
    ) -> torch.Tensor:
        """
        Encode a batch of toponyms to embeddings.

        Args:
            toponyms: List of toponym strings or (name, lang) tuples
            batch_size: Processing batch size
            show_progress: Whether to show progress bar

        Returns:
            Embedding tensor of shape [N, embed_dim]
        """
        # Normalize input format (safely handle various input types)
        normalized = []
        for item in toponyms:
            if isinstance(item, str):
                normalized.append((item, None))
            elif isinstance(item, (list, tuple)):
                name = item[0] if len(item) > 0 else ''
                lang = item[1] if len(item) > 1 else None
                normalized.append((name, lang))
            else:
                logger.warning(f"Skipping invalid input: {item}")
                continue

        # Prepare all inputs
        all_char_ids = []
        all_script_ids = []
        all_lang_ids = []

        for name, lang in normalized:
            char_ids, script_id, lang_id = self._prepare_input(name, lang)
            all_char_ids.append(char_ids)
            all_script_ids.append(script_id)
            all_lang_ids.append(lang_id)

        # Process in batches
        all_embeddings = []

        iterator = range(0, len(normalized), batch_size)
        if show_progress:
            try:
                from tqdm import tqdm
                iterator = tqdm(iterator, desc="Encoding", total=len(normalized) // batch_size + 1)
            except ImportError:
                pass

        for start_idx in iterator:
            end_idx = min(start_idx + batch_size, len(normalized))

            # Get batch data
            batch_char_ids = all_char_ids[start_idx:end_idx]
            batch_script_ids = all_script_ids[start_idx:end_idx]
            batch_lang_ids = all_lang_ids[start_idx:end_idx]

            # Pad character sequences
            lengths = [len(ids) for ids in batch_char_ids]
            max_len = max(lengths)

            padded = torch.zeros(len(batch_char_ids), max_len, dtype=torch.long, device=self.device)
            for i, ids in enumerate(batch_char_ids):
                padded[i, :len(ids)] = torch.tensor(ids, dtype=torch.long)

            # Create tensors
            script_ids_t = torch.tensor(batch_script_ids, dtype=torch.long, device=self.device)
            lang_ids_t = torch.tensor(batch_lang_ids, dtype=torch.long, device=self.device)
            lengths_t = torch.tensor(lengths, dtype=torch.long)

            # Forward pass
            with torch.no_grad():
                embeddings = self.model(padded, script_ids_t, lang_ids_t, lengths_t)

            all_embeddings.append(embeddings)

        return torch.cat(all_embeddings, dim=0)

    def similarity(
            self,
            query: torch.Tensor,
            candidates: torch.Tensor,
    ) -> torch.Tensor:
        """
        Compute cosine similarity between query and candidates.

        Args:
            query: Query embedding [embed_dim] or [1, embed_dim]
            candidates: Candidate embeddings [N, embed_dim]

        Returns:
            Similarity scores [N]
        """
        if query.dim() == 1:
            query = query.unsqueeze(0)

        return F.cosine_similarity(query, candidates, dim=-1)

    def find_similar(
            self,
            query: str,
            candidates: List[str],
            top_k: int = 10,
            lang: Optional[str] = None,
    ) -> List[Tuple[str, float]]:
        """
        Find most similar toponyms from a list of candidates.

        Args:
            query: Query toponym
            candidates: List of candidate toponyms
            top_k: Number of results to return
            lang: Optional language code for query

        Returns:
            List of (toponym, similarity_score) tuples, sorted by similarity
        """
        query_emb = self.encode(query, lang=lang)
        candidate_embs = self.encode_batch(candidates)

        similarities = self.similarity(query_emb, candidate_embs)

        # Get top-k indices
        top_k = min(top_k, len(candidates))
        top_indices = similarities.argsort(descending=True)[:top_k]

        results = []
        for idx in top_indices:
            results.append((candidates[idx], similarities[idx].item()))

        return results

    @property
    def embed_dim(self) -> int:
        """Get embedding dimension."""
        return self.model.embed_dim

    def to(self, device: str) -> 'ToponymEncoder':
        """Move encoder to device."""
        self.device = device
        self.model = self.model.to(device)
        return self


class ESIndexUpdater:
    """
    Update Elasticsearch index with phonetic embeddings.

    Scans the toponyms index and populates the embedding field
    using the trained model.
    """

    def __init__(
            self,
            encoder: ToponymEncoder,
            es_client,
            index: str = 'toponyms',
            embedding_version: int = 2,
    ):
        self.encoder = encoder
        self.es = es_client
        self.index = index
        self.embedding_version = embedding_version

    def update_all(
            self,
            batch_size: int = 500,
            scroll_size: int = 1000,
            show_progress: bool = True,
            force_update: bool = False,
    ) -> Dict[str, int]:
        """
        Update toponyms in the index with embeddings.

        Skips documents that already have the current embedding_version
        unless force_update=True.

        Args:
            batch_size: Number of toponyms to encode at once
            scroll_size: ES scroll batch size
            show_progress: Whether to show progress bar
            force_update: If True, re-process all documents

        Returns:
            Statistics dict with counts
        """
        from elasticsearch.helpers import scan, bulk

        # Get total count
        total_docs = self.es.count(index=self.index)['count']

        # Build query - skip already processed unless forced
        if force_update:
            query = {"query": {"match_all": {}}}
        else:
            # Only fetch docs where version is missing OR version != current
            query = {
                "query": {
                    "bool": {
                        "must_not": [
                            {"term": {"embedding_version": self.embedding_version}}
                        ]
                    }
                }
            }

        # Count remaining work
        remaining = self.es.count(index=self.index, body=query)['count']

        if remaining == 0:
            logger.info("All documents are already up to date.")
            return {'processed': 0, 'updated': 0, 'errors': 0}

        logger.info(
            f"Updating embeddings for {remaining:,} / {total_docs:,} toponyms (Version {self.embedding_version})")

        stats = {
            'processed': 0,
            'updated': 0,
            'errors': 0,
        }

        # Add source selection to query
        query["_source"] = ["toponym_id", "name", "lang", "script"]

        buffer = []

        iterator = scan(
            self.es,
            index=self.index,
            query=query,
            scroll='60m',  # Increased scroll time for safety
            size=scroll_size,
        )

        if show_progress:
            try:
                from tqdm import tqdm
                iterator = tqdm(iterator, total=remaining, desc="Updating embeddings")
            except ImportError:
                pass

        for doc in iterator:
            doc_id = doc['_id']
            source = doc['_source']

            name = source.get('name', '')
            lang = source.get('lang')
            script_str = source.get('script', 'OTHER')

            try:
                script = Script(script_str)
            except ValueError:
                script = Script.OTHER

            buffer.append({
                'doc_id': doc_id,
                'name': name,
                'lang': lang,
                'script': script,
            })

            # Process batch
            if len(buffer) >= batch_size:
                self._process_batch(buffer, stats)
                buffer = []

        # Process remaining
        if buffer:
            self._process_batch(buffer, stats)

        # Refresh index
        self.es.indices.refresh(index=self.index)

        logger.info(f"Update complete: {stats}")
        return stats

    def _process_batch(self, buffer: List[Dict], stats: Dict):
        """Process a batch of toponyms."""
        from elasticsearch.helpers import bulk

        # Prepare inputs
        names = [item['name'] for item in buffer]
        langs = [item['lang'] for item in buffer]
        scripts = [item['script'] for item in buffer]

        # Encode batch
        inputs = list(zip(names, langs))
        embeddings = self.encoder.encode_batch(inputs)

        # Prepare bulk updates
        actions = []
        for i, item in enumerate(buffer):
            embedding = embeddings[i].cpu().tolist()

            actions.append({
                '_op_type': 'update',
                '_index': self.index,
                '_id': item['doc_id'],
                'doc': {
                    'embedding': embedding,
                    'embedding_version': self.embedding_version,
                }
            })

        # Execute bulk update
        success, errors = bulk(self.es, actions, raise_on_error=False)

        stats['processed'] += len(buffer)
        stats['updated'] += success
        stats['errors'] += len(errors) if errors else 0

    def update_subset(
            self,
            toponym_ids: List[str],
            batch_size: int = 500,
    ) -> Dict[str, int]:
        """
        Update embeddings for a specific list of toponym IDs.

        Args:
            toponym_ids: List of toponym IDs to update
            batch_size: Processing batch size

        Returns:
            Statistics dict
        """
        from elasticsearch.helpers import bulk

        stats = {'processed': 0, 'updated': 0, 'errors': 0}

        for start_idx in range(0, len(toponym_ids), batch_size):
            end_idx = min(start_idx + batch_size, len(toponym_ids))
            batch_ids = toponym_ids[start_idx:end_idx]

            # Fetch documents
            response = self.es.mget(
                index=self.index,
                body={'ids': batch_ids},
                _source=['name', 'lang', 'script']
            )

            buffer = []
            for doc in response['docs']:
                if not doc.get('found'):
                    continue

                source = doc['_source']
                script_str = source.get('script', 'OTHER')
                try:
                    script = Script(script_str)
                except ValueError:
                    script = Script.OTHER

                buffer.append({
                    'doc_id': doc['_id'],
                    'name': source.get('name', ''),
                    'lang': source.get('lang'),
                    'script': script,
                })

            if buffer:
                self._process_batch(buffer, stats)

        return stats


def search_similar(
        es_client,
        query_embedding: List[float],
        index: str = 'toponyms',
        top_k: int = 10,
        min_score: float = 0.5,
        filters: Optional[Dict] = None,
) -> List[Dict]:
    """
    Search for similar toponyms using vector similarity.

    Args:
        es_client: Elasticsearch client
        query_embedding: Query embedding vector
        index: Index to search
        top_k: Number of results
        min_score: Minimum similarity score
        filters: Optional additional filters

    Returns:
        List of matching documents with scores
    """
    # Build query
    script_query = {
        "script_score": {
            "query": filters if filters else {"match_all": {}},
            "script": {
                "source": "cosineSimilarity(params.query_vector, 'embedding') + 1.0",
                "params": {"query_vector": query_embedding}
            }
        }
    }

    response = es_client.search(
        index=index,
        body={
            "size": top_k,
            "query": script_query,
            "_source": ["toponym_id", "name", "lang", "script", "namespaces"],
            "min_score": min_score + 1.0,  # Adjust for the +1.0 in script
        }
    )

    results = []
    for hit in response['hits']['hits']:
        results.append({
            'toponym_id': hit['_source'].get('toponym_id'),
            'name': hit['_source'].get('name'),
            'lang': hit['_source'].get('lang'),
            'script': hit['_source'].get('script'),
            'namespaces': hit['_source'].get('namespaces', []),
            'score': hit['_score'] - 1.0,  # Adjust back to cosine similarity
        })

    return results


# Convenience function for quick encoding
def encode_toponym(
        name: str,
        checkpoint_path: str,
        vocab_dir: str,
        lang: Optional[str] = None,
        device: str = 'cpu',
) -> List[float]:
    """
    Quick utility to encode a single toponym.

    Args:
        name: Toponym string
        checkpoint_path: Path to model checkpoint
        vocab_dir: Path to vocabulary directory
        lang: Optional language code
        device: Device to use

    Returns:
        Embedding as list of floats
    """
    encoder = ToponymEncoder.from_checkpoint(checkpoint_path, vocab_dir, device)
    embedding = encoder.encode(name, lang=lang)
    return embedding.cpu().tolist()