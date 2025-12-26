"""
Phonetic Similarity Model for Multilingual Toponym Matching

A Student-Teacher architecture that learns phonetic embeddings from toponyms.
- Teacher: Epitran + PanPhon → IPA features → BiLSTM + Self-Attention (phonetically grounded)
- Student: anyascii + Language ID → BiLSTM + Self-Attention (universal fallback)

Upgraded Architecture (v2):
- BiLSTM + Lightweight Self-Attention (1-2 heads)
- Attention-Aware Pooling (replaces mean/last-state pooling)
- Curriculum Hard Negatives for Phase 3

Training proceeds in three phases:
1. Train Teacher on IPA features (triplet loss)
2. Align Student to Teacher (MSE + cosine loss)
3. Fine-tune Student with curriculum hard negatives (triplet loss)
"""

from .config import Config
from .vocab import CharVocab, LangVocab
from .models import (
    PhoneticEncoder,
    CharEncoder,
    HybridPhoneticModel,
    SelfAttention,
    AttentionPooling
)
from .losses import TripletLoss, RobustAlignmentLoss
from .inference import PhoneticSimilarityModel

__version__ = "2.0.0"
__all__ = [
    "Config",
    "CharVocab",
    "LangVocab",
    "PhoneticEncoder",
    "CharEncoder",
    "HybridPhoneticModel",
    "SelfAttention",
    "AttentionPooling",
    "TripletLoss",
    "RobustAlignmentLoss",
    "PhoneticSimilarityModel",
]
