"""
Production Inference Wrapper for Phonetic Similarity Model.

Provides a clean API for:
- Single toponym embedding
- Pairwise similarity computation
- Batch embedding
- Similarity search
"""

import os
from typing import List, Optional, Tuple

import numpy as np
import torch

try:
    import epitran
    from panphon import FeatureTable
except ImportError:
    raise ImportError("Please install epitran and panphon: pip install epitran panphon")

try:
    from anyascii import anyascii
except ImportError:
    raise ImportError("Please install anyascii: pip install anyascii")

from .config import Config
from .vocab import CharVocab, LangVocab
from .models import PhoneticEncoder, CharEncoder, HybridPhoneticModel


class PhoneticSimilarityModel:
    """
    Production inference wrapper.
    
    Handles both Epitran-supported and unsupported languages transparently.
    Uses phonetic pathway when available, character pathway as fallback,
    and gated fusion when both are present.
    
    Example:
        model = PhoneticSimilarityModel('final_model.pt')
        
        # Get similarity score
        sim = model.similarity('London', 'en', 'Londres', 'fr')
        print(f"Similarity: {sim:.3f}")
        
        # Get embedding
        emb = model.embed('東京', 'ja')
        
        # Batch embedding
        embeddings = model.batch_embed([
            ('London', 'en'),
            ('Londres', 'fr'),
            ('Londra', 'it')
        ])
    """

    def __init__(self, model_path: str, device: str = 'cpu'):
        self.device = torch.device(device)
        
        # Load model checkpoint
        checkpoint = torch.load(model_path, map_location=self.device)
        
        # Load vocabularies
        model_dir = os.path.dirname(model_path) or '.'
        base_name = os.path.splitext(os.path.basename(model_path))[0]
        
        self.char_vocab = CharVocab.load(os.path.join(model_dir, f'{base_name}_char_vocab.pkl'))
        self.lang_vocab = LangVocab.load(os.path.join(model_dir, f'{base_name}_lang_vocab.pkl'))
        
        # Create model
        phonetic_encoder = PhoneticEncoder()
        char_encoder = CharEncoder(
            vocab_size=checkpoint.get('char_vocab_size', self.char_vocab.vocab_size),
            num_langs=checkpoint.get('num_langs', self.lang_vocab.next_id)
        )
        
        self.model = HybridPhoneticModel(phonetic_encoder, char_encoder)
        self.model.load_state_dict(checkpoint['model_state'])
        self.model.to(self.device)
        self.model.eval()
        
        # Epitran instances (lazy loading)
        self._epi_cache = {}
        self.ft = FeatureTable()
        
        print(f"Model loaded from {model_path}")
        print(f"  Char vocab: {len(self.char_vocab.char_to_id)} chars")
        print(f"  Lang vocab: {len(self.lang_vocab.lang_to_id)} languages")

    def _get_epitran(self, lang: str) -> Optional[epitran.Epitran]:
        """Get or create Epitran instance for a language."""
        if lang not in self._epi_cache:
            code = Config.EPITRAN_LANGS.get(lang)
            if code:
                try:
                    self._epi_cache[lang] = epitran.Epitran(code)
                except Exception:
                    self._epi_cache[lang] = None
            else:
                self._epi_cache[lang] = None
        return self._epi_cache[lang]

    def embed(self, toponym: str, lang: str) -> np.ndarray:
        """
        Get embedding for a toponym@lang pair.
        
        Uses phonetic pathway when Epitran supports the language,
        character pathway otherwise, gated fusion when both available.
        
        Args:
            toponym: The place name
            lang: ISO 639-1 language code
        
        Returns:
            64-dimensional normalized embedding as numpy array
        """
        # Romanize (always available)
        romanized = anyascii(toponym).lower().strip()
        char_ids = torch.tensor([self.char_vocab.encode(romanized)], dtype=torch.long)
        char_lengths = torch.tensor([char_ids.size(1)])
        lang_ids = torch.tensor([self.lang_vocab.encode(lang)], dtype=torch.long)
        
        # Try phonetic pathway
        phonetic_seq = None
        phonetic_lengths = None
        has_phonetic = torch.tensor([False])
        
        epi = self._get_epitran(lang)
        if epi:
            try:
                ipa = epi.transliterate(toponym)
                features = self.ft.word_to_vector_list(ipa, numeric=True)
                if features:
                    phonetic_seq = torch.tensor([features], dtype=torch.float32)
                    phonetic_lengths = torch.tensor([len(features)])
                    has_phonetic = torch.tensor([True])
            except Exception:
                pass
        
        with torch.no_grad():
            embedding = self.model(
                char_ids.to(self.device),
                lang_ids.to(self.device),
                char_lengths,
                phonetic_seq.to(self.device) if phonetic_seq is not None else None,
                phonetic_lengths,
                has_phonetic.to(self.device)
            )
        
        return embedding.cpu().numpy()[0]

    def similarity(
        self,
        toponym_a: str,
        lang_a: str,
        toponym_b: str,
        lang_b: str
    ) -> float:
        """
        Compute cosine similarity between two toponyms.
        
        Args:
            toponym_a: First place name
            lang_a: Language of first place name
            toponym_b: Second place name
            lang_b: Language of second place name
        
        Returns:
            Cosine similarity score in range [-1, 1]
        """
        emb_a = self.embed(toponym_a, lang_a)
        emb_b = self.embed(toponym_b, lang_b)
        return float(np.dot(emb_a, emb_b))

    def batch_embed(self, toponyms_and_langs: List[Tuple[str, str]]) -> np.ndarray:
        """
        Batch embedding for multiple toponyms.
        
        Args:
            toponyms_and_langs: List of (toponym, lang) tuples
        
        Returns:
            (N, 64) array of embeddings
        """
        embeddings = []
        for toponym, lang in toponyms_and_langs:
            embeddings.append(self.embed(toponym, lang))
        return np.array(embeddings)

    def find_similar(
        self,
        query_toponym: str,
        query_lang: str,
        candidates: List[Tuple[str, str]],
        top_k: int = 10
    ) -> List[Tuple[str, str, float]]:
        """
        Find most similar toponyms from candidates.
        
        Args:
            query_toponym: Query place name
            query_lang: Query language
            candidates: List of (toponym, lang) candidates
            top_k: Number of results to return
        
        Returns:
            List of (toponym, lang, similarity) tuples, sorted by similarity descending
        """
        query_emb = self.embed(query_toponym, query_lang)
        candidate_embs = self.batch_embed(candidates)
        
        similarities = candidate_embs @ query_emb
        
        indices = np.argsort(similarities)[::-1][:top_k]
        
        results = []
        for idx in indices:
            toponym, lang = candidates[idx]
            results.append((toponym, lang, float(similarities[idx])))
        
        return results
