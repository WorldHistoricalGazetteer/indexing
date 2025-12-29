"""
Neural Network Models for Phonetic Similarity.

Architecture (v2):
- BiLSTM + Lightweight Self-Attention (1-2 heads)
- Attention-Aware Pooling (replaces mean/last-state pooling)

Key Changes from v1:
1. Self-Attention operates over timesteps (not features)
2. Attention weights used for pooling (weighted sum, not sequence)
3. No positional encoding (BiLSTM already encodes order)
"""

import math
from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F

from .config import Config


class SelfAttention(nn.Module):
    """
    Lightweight Multi-Head Self-Attention for sequence modeling.
    
    Operates over timesteps to learn which phonetic segments matter most.
    Uses 1-2 attention heads as specified in requirements.
    
    Key design choices:
    - No positional encoding (BiLSTM already encodes order)
    - Attention dimension equals BiLSTM hidden size
    - Output is attention weights for pooling, not a transformed sequence
    """

    def __init__(
        self,
        hidden_dim: int,
        num_heads: int = Config.NUM_ATTENTION_HEADS,
        dropout: float = Config.ATTENTION_DROPOUT
    ):
        super().__init__()
        
        assert hidden_dim % num_heads == 0, \
            f"hidden_dim ({hidden_dim}) must be divisible by num_heads ({num_heads})"
        
        self.hidden_dim = hidden_dim
        self.num_heads = num_heads
        self.head_dim = hidden_dim // num_heads
        self.scale = math.sqrt(self.head_dim)
        
        # Linear projections for Q, K, V
        self.q_proj = nn.Linear(hidden_dim, hidden_dim)
        self.k_proj = nn.Linear(hidden_dim, hidden_dim)
        self.v_proj = nn.Linear(hidden_dim, hidden_dim)
        
        # Output projection
        self.out_proj = nn.Linear(hidden_dim, hidden_dim)
        
        self.dropout = nn.Dropout(dropout)

    def forward(
        self,
        x: torch.Tensor,
        mask: Optional[torch.Tensor] = None
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """
        Apply self-attention over timesteps.
        
        Args:
            x: (batch, seq_len, hidden_dim) - BiLSTM output
            mask: (batch, seq_len) - True for valid positions, False for padding
        
        Returns:
            attended: (batch, seq_len, hidden_dim) - Attended sequence
            attn_weights: (batch, num_heads, seq_len, seq_len) - Attention weights
        """
        batch_size, seq_len, _ = x.shape
        
        # Project to Q, K, V
        Q = self.q_proj(x)  # (batch, seq_len, hidden_dim)
        K = self.k_proj(x)
        V = self.v_proj(x)
        
        # Reshape for multi-head attention
        # (batch, seq_len, num_heads, head_dim) -> (batch, num_heads, seq_len, head_dim)
        Q = Q.view(batch_size, seq_len, self.num_heads, self.head_dim).transpose(1, 2)
        K = K.view(batch_size, seq_len, self.num_heads, self.head_dim).transpose(1, 2)
        V = V.view(batch_size, seq_len, self.num_heads, self.head_dim).transpose(1, 2)
        
        # Compute attention scores
        # (batch, num_heads, seq_len, seq_len)
        attn_scores = torch.matmul(Q, K.transpose(-2, -1)) / self.scale
        
        # Apply mask if provided (for padding)
        if mask is not None:
            # Expand mask: (batch, seq_len) -> (batch, 1, 1, seq_len)
            mask = mask.unsqueeze(1).unsqueeze(2)
            attn_scores = attn_scores.masked_fill(~mask, float('-inf'))
        
        # Softmax and dropout
        attn_weights = F.softmax(attn_scores, dim=-1)
        attn_weights = self.dropout(attn_weights)
        
        # Apply attention to values
        # (batch, num_heads, seq_len, head_dim)
        attended = torch.matmul(attn_weights, V)
        
        # Reshape back
        # (batch, seq_len, hidden_dim)
        attended = attended.transpose(1, 2).contiguous().view(batch_size, seq_len, self.hidden_dim)
        
        # Output projection
        attended = self.out_proj(attended)
        
        return attended, attn_weights


class AttentionPooling(nn.Module):
    """
    Attention-Aware Pooling Layer.
    
    Replaces mean/last-state pooling with learned attention-weighted pooling.
    Produces a fixed-length vector from variable-length sequences.
    
    Key benefits:
    - Sharpens embeddings by focusing on salient phonemes
    - Prevents dilution by low-salience phones/characters
    - Learns which positions matter for phonetic similarity
    """

    def __init__(self, hidden_dim: int, dropout: float = Config.DROPOUT):
        super().__init__()
        
        # Learnable attention query vector
        self.attention_query = nn.Parameter(torch.randn(hidden_dim))
        
        # Projection for computing attention scores
        self.attention_proj = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim),
            nn.Tanh(),
            nn.Linear(hidden_dim, 1)
        )
        
        self.dropout = nn.Dropout(dropout)

    def forward(
        self,
        x: torch.Tensor,
        mask: Optional[torch.Tensor] = None
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """
        Pool sequence using learned attention weights.
        
        Args:
            x: (batch, seq_len, hidden_dim) - Sequence to pool
            mask: (batch, seq_len) - True for valid positions
        
        Returns:
            pooled: (batch, hidden_dim) - Fixed-length pooled representation
            attn_weights: (batch, seq_len) - Attention weights used for pooling
        """
        # Compute attention scores
        # (batch, seq_len, 1) -> (batch, seq_len)
        attn_scores = self.attention_proj(x).squeeze(-1)
        
        # Apply mask if provided
        if mask is not None:
            attn_scores = attn_scores.masked_fill(~mask, float('-inf'))
        
        # Softmax to get weights
        attn_weights = F.softmax(attn_scores, dim=-1)
        attn_weights = self.dropout(attn_weights)
        
        # Weighted sum: (batch, seq_len, 1) * (batch, seq_len, hidden_dim)
        # -> (batch, hidden_dim)
        pooled = torch.bmm(attn_weights.unsqueeze(1), x).squeeze(1)
        
        return pooled, attn_weights


class PhoneticEncoder(nn.Module):
    """
    Teacher Model: Pure phonetic (IPA) encoder.
    
    Architecture (v2):
        PanPhon features → BiLSTM → Self-Attention → Attention Pooling → Projection
    
    Takes PanPhon feature vectors and produces normalized embeddings.
    """

    def __init__(
        self,
        phonetic_feat_dim: int = Config.PHONETIC_FEAT_DIM,
        hidden_dim: int = Config.HIDDEN_DIM,
        embed_dim: int = Config.EMBED_DIM,
        num_layers: int = Config.NUM_LAYERS,
        num_attention_heads: int = Config.NUM_ATTENTION_HEADS,
        dropout: float = Config.DROPOUT
    ):
        super().__init__()
        
        self.hidden_dim = hidden_dim
        
        # BiLSTM encoder
        self.bilstm = nn.LSTM(
            input_size=phonetic_feat_dim,
            hidden_size=hidden_dim,
            num_layers=num_layers,
            batch_first=True,
            bidirectional=True,
            dropout=dropout if num_layers > 1 else 0
        )
        
        # Self-Attention (operates on BiLSTM output, which is hidden_dim * 2 for bidirectional)
        self.self_attention = SelfAttention(
            hidden_dim=hidden_dim * 2,
            num_heads=num_attention_heads,
            dropout=dropout
        )
        
        # Attention-Aware Pooling
        self.pooling = AttentionPooling(
            hidden_dim=hidden_dim * 2,
            dropout=dropout
        )
        
        # Projection to embedding space
        self.projection = nn.Sequential(
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, embed_dim)
        )

    def forward(
        self,
        phonetic_seq: torch.Tensor,
        seq_lengths: torch.Tensor
    ) -> torch.Tensor:
        """
        Encode phonetic sequences to embeddings.
        
        Args:
            phonetic_seq: (batch, max_seq_len, phonetic_feat_dim) [GPU]
            seq_lengths: (batch,) [CPU - keeps on CPU for pack_padded_sequence]

        Returns:
            (batch, embed_dim) normalized embeddings
        """
        batch_size = phonetic_seq.size(0)
        max_len = phonetic_seq.size(1)
        device = phonetic_seq.device

        # Create mask - move lengths to GPU only for this (async copy)
        mask = torch.arange(max_len, device=device).unsqueeze(0) < seq_lengths.to(device).unsqueeze(1)

        # Pack with CPU lengths (no sync needed)
        packed = nn.utils.rnn.pack_padded_sequence(
            phonetic_seq, seq_lengths,
            batch_first=True, enforce_sorted=False
        )
        lstm_out, _ = self.bilstm(packed)
        lstm_out, _ = nn.utils.rnn.pad_packed_sequence(lstm_out, batch_first=True, total_length=max_len)

        # Self-Attention over timesteps
        attended, _ = self.self_attention(lstm_out, mask)

        # Residual connection
        attended = attended + lstm_out

        # Attention-Aware Pooling
        pooled, _ = self.pooling(attended, mask)

        # Project to embedding space
        embedding = self.projection(pooled)

        return F.normalize(embedding, p=2, dim=-1)


class CharEncoder(nn.Module):
    """
    Student Model: Language-Conditioned Character Encoder.

    Architecture (v2):
        Char Embed + Lang Embed → BiLSTM → Self-Attention → Attention Pooling → Projection

    Learns to approximate phonetic space from (Romanized Text + Language ID).
    The language embedding is concatenated at every timestep to condition
    the LSTM on the source language.
    """

    def __init__(
        self,
        vocab_size: int = Config.VOCAB_SIZE,
        num_langs: int = Config.NUM_LANGS,
        char_embed_dim: int = Config.CHAR_EMBED_DIM,
        lang_embed_dim: int = Config.LANG_EMBED_DIM,
        hidden_dim: int = Config.HIDDEN_DIM,
        embed_dim: int = Config.EMBED_DIM,
        num_layers: int = Config.NUM_LAYERS,
        num_attention_heads: int = Config.NUM_ATTENTION_HEADS,
        dropout: float = Config.DROPOUT
    ):
        super().__init__()

        self.hidden_dim = hidden_dim

        # Embeddings
        self.char_embed = nn.Embedding(vocab_size, char_embed_dim, padding_idx=0)
        self.lang_embed = nn.Embedding(num_langs, lang_embed_dim)

        # BiLSTM: input is char embedding + language embedding at each timestep
        self.bilstm = nn.LSTM(
            input_size=char_embed_dim + lang_embed_dim,
            hidden_size=hidden_dim,
            num_layers=num_layers,
            batch_first=True,
            bidirectional=True,
            dropout=dropout if num_layers > 1 else 0
        )

        # Self-Attention
        self.self_attention = SelfAttention(
            hidden_dim=hidden_dim * 2,
            num_heads=num_attention_heads,
            dropout=dropout
        )

        # Attention-Aware Pooling
        self.pooling = AttentionPooling(
            hidden_dim=hidden_dim * 2,
            dropout=dropout
        )

        # Projection to embedding space
        self.projection = nn.Sequential(
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, embed_dim)
        )

    def forward(
        self,
        char_ids: torch.Tensor,
        lang_ids: torch.Tensor,
        seq_lengths: torch.Tensor
    ) -> torch.Tensor:
        """
        Encode character sequences with language conditioning.

        Args:
            char_ids: (batch, max_seq_len) - romanized character IDs [GPU]
            lang_ids: (batch,) - language IDs [GPU]
            seq_lengths: (batch,) - actual sequence lengths [CPU]

        Returns:
            (batch, embed_dim) normalized embeddings
        """
        batch_size, max_len = char_ids.shape
        device = char_ids.device

        # Create mask - move lengths to GPU only for this (async copy)
        mask = torch.arange(max_len, device=device).unsqueeze(0) < seq_lengths.to(device).unsqueeze(1)

        # Embed characters: (batch, seq, char_embed_dim)
        c_emb = self.char_embed(char_ids)

        # Embed language and broadcast: (batch, seq, lang_embed_dim)
        l_emb = self.lang_embed(lang_ids)
        l_emb = l_emb.unsqueeze(1).expand(-1, max_len, -1)

        # Concatenate: (batch, seq, char_embed_dim + lang_embed_dim)
        combined_input = torch.cat([c_emb, l_emb], dim=-1)

        # Pack with CPU lengths (no sync needed)
        packed = nn.utils.rnn.pack_padded_sequence(
            combined_input, seq_lengths,
            batch_first=True, enforce_sorted=False
        )
        lstm_out, _ = self.bilstm(packed)
        lstm_out, _ = nn.utils.rnn.pad_packed_sequence(lstm_out, batch_first=True, total_length=max_len)

        # Self-Attention over timesteps
        attended, _ = self.self_attention(lstm_out, mask)

        # Residual connection
        attended = attended + lstm_out

        # Attention-Aware Pooling
        pooled, _ = self.pooling(attended, mask)

        # Project and normalize
        embedding = self.projection(pooled)

        return F.normalize(embedding, p=2, dim=-1)


class HybridPhoneticModel(nn.Module):
    """
    Full hybrid model combining Teacher (phonetic) and Student (character) pathways.
    Uses gated fusion when both pathways are available.
    """

    def __init__(
        self,
        phonetic_encoder: PhoneticEncoder,
        char_encoder: CharEncoder,
        embed_dim: int = Config.EMBED_DIM
    ):
        super().__init__()

        self.phonetic_encoder = phonetic_encoder
        self.char_encoder = char_encoder

        # Learnable gate for blending pathways
        self.gate = nn.Sequential(
            nn.Linear(embed_dim * 2, embed_dim),
            nn.ReLU(),
            nn.Linear(embed_dim, 1),
            nn.Sigmoid()
        )

    def forward(
        self,
        char_ids: torch.Tensor,
        lang_ids: torch.Tensor,
        char_lengths: torch.Tensor,
        phonetic_seq: Optional[torch.Tensor] = None,
        phonetic_lengths: Optional[torch.Tensor] = None,
        has_phonetic: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        """
        Forward pass with optional phonetic pathway.

        Args:
            char_ids: (batch, max_char_len) - romanized character IDs
            lang_ids: (batch,) - language IDs
            char_lengths: (batch,) - character sequence lengths
            phonetic_seq: (batch, max_phone_len, feat_dim) - IPA features, optional
            phonetic_lengths: (batch,) - phonetic sequence lengths, optional
            has_phonetic: (batch,) - boolean mask for items with phonetic features

        Returns:
            (batch, embed_dim) normalized embeddings
        """
        # Character pathway (always available)
        char_emb = self.char_encoder(char_ids, lang_ids, char_lengths)

        # Phonetic pathway (when available)
        if phonetic_seq is not None and has_phonetic is not None and has_phonetic.any():
            phone_emb = torch.zeros_like(char_emb)

            # Only process items with phonetic features
            mask = has_phonetic
            if mask.any():
                phone_subset = self.phonetic_encoder(
                    phonetic_seq[mask],
                    phonetic_lengths[mask]
                )
                phone_emb[mask] = phone_subset

            # Gated fusion
            combined = torch.cat([char_emb, phone_emb], dim=-1)
            gate_value = self.gate(combined)

            # Apply gate only where we have phonetic
            gate_value = gate_value * has_phonetic.float().unsqueeze(-1)
            fused = gate_value * phone_emb + (1 - gate_value) * char_emb

            return F.normalize(fused, p=2, dim=-1)
        else:
            return char_emb

    def encode_phonetic_only(
        self,
        phonetic_seq: torch.Tensor,
        phonetic_lengths: torch.Tensor
    ) -> torch.Tensor:
        """Direct phonetic encoding (for Phase 1 training)."""
        return self.phonetic_encoder(phonetic_seq, phonetic_lengths)

    def encode_char_only(
        self,
        char_ids: torch.Tensor,
        lang_ids: torch.Tensor,
        char_lengths: torch.Tensor
    ) -> torch.Tensor:
        """Direct character encoding (for Phase 3 training)."""
        return self.char_encoder(char_ids, lang_ids, char_lengths)