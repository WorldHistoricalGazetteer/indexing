# models/models.py
"""
Phonetic embedding models v2.

This module implements the redesigned architecture for universal toponym matching:

1. PhoneticEncoder (Teacher): Encodes IPA/PanPhon features to 128-dim embeddings
2. UniversalEncoder (Student): Script-aware character encoder with language conditioning
3. HybridPhoneticModel: Gated combination for inference

Key improvements over v1:
- 128-dimensional embeddings (up from 64)
- Script-partitioned character vocabulary (native script reading)
- Language embedding with dropout for unknown language robustness
- Noise-aware training (typo tolerance)
- Attention-aware pooling preserved from v1

Training phases:
- Phase 1: Train Teacher on phonetic features (contrastive)
- Phase 2: Align Student to Teacher (with noise augmentation, language dropout)
- Phase 3: Fine-tune Student with hard negatives (contrastive)
"""

import math
from typing import Optional, Tuple, Dict, Any

import torch
import torch.nn as nn
import torch.nn.functional as F


# ============================================================================
# Attention Mechanisms
# ============================================================================

class SelfAttention(nn.Module):
    """
    Multi-head self-attention for sequence modeling.

    Applies attention over BiLSTM outputs to capture long-range dependencies
    between characters in a toponym.
    """

    def __init__(
            self,
            hidden_dim: int,
            num_heads: int = 2,
            dropout: float = 0.1,
    ):
        super().__init__()

        assert hidden_dim % num_heads == 0, "hidden_dim must be divisible by num_heads"

        self.hidden_dim = hidden_dim
        self.num_heads = num_heads
        self.head_dim = hidden_dim // num_heads
        self.scale = math.sqrt(self.head_dim)

        self.q_proj = nn.Linear(hidden_dim, hidden_dim)
        self.k_proj = nn.Linear(hidden_dim, hidden_dim)
        self.v_proj = nn.Linear(hidden_dim, hidden_dim)
        self.out_proj = nn.Linear(hidden_dim, hidden_dim)

        self.dropout = nn.Dropout(dropout)

    def forward(
            self,
            x: torch.Tensor,
            mask: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Apply multi-head self-attention.

        Args:
            x: Input tensor [B, L, H]
            mask: Boolean mask [B, L] where True = valid position

        Returns:
            Tuple of (attended output [B, L, H], attention weights [B, heads, L, L])
        """
        batch_size, seq_len, _ = x.shape

        # Project to Q, K, V
        q = self.q_proj(x).view(batch_size, seq_len, self.num_heads, self.head_dim).transpose(1, 2)
        k = self.k_proj(x).view(batch_size, seq_len, self.num_heads, self.head_dim).transpose(1, 2)
        v = self.v_proj(x).view(batch_size, seq_len, self.num_heads, self.head_dim).transpose(1, 2)

        # Attention scores
        scores = torch.matmul(q, k.transpose(-2, -1)) / self.scale  # [B, heads, L, L]

        # Apply mask
        if mask is not None:
            # Expand mask for attention: [B, L] -> [B, 1, 1, L]
            mask_expanded = mask.unsqueeze(1).unsqueeze(2)
            scores = scores.masked_fill(~mask_expanded, float('-inf'))

        attn_weights = F.softmax(scores, dim=-1)
        attn_weights = self.dropout(attn_weights)

        # Apply attention
        attended = torch.matmul(attn_weights, v)  # [B, heads, L, head_dim]
        attended = attended.transpose(1, 2).contiguous().view(batch_size, seq_len, self.hidden_dim)

        output = self.out_proj(attended)

        return output, attn_weights


class AttentionPooling(nn.Module):
    """
    Attention-based pooling to aggregate sequence into fixed-size vector.

    Learns to weight positions by importance rather than using simple
    mean/max pooling. This preserves more information about which
    characters are phonetically significant.
    """

    def __init__(
            self,
            hidden_dim: int,
            dropout: float = 0.1,
    ):
        super().__init__()

        self.attention = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim // 2),
            nn.Tanh(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim // 2, 1),
        )

    def forward(
            self,
            x: torch.Tensor,
            mask: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Pool sequence to single vector using learned attention.

        Args:
            x: Input tensor [B, L, H]
            mask: Boolean mask [B, L] where True = valid position

        Returns:
            Tuple of (pooled output [B, H], attention weights [B, L])
        """
        # Compute attention scores
        scores = self.attention(x).squeeze(-1)  # [B, L]

        # Mask invalid positions
        if mask is not None:
            scores = scores.masked_fill(~mask, float('-inf'))

        attn_weights = F.softmax(scores, dim=-1)  # [B, L]

        # Weighted sum
        pooled = torch.bmm(attn_weights.unsqueeze(1), x).squeeze(1)  # [B, H]

        return pooled, attn_weights


# ============================================================================
# Teacher Model: PhoneticEncoder
# ============================================================================

class PhoneticEncoder(nn.Module):
    """
    Teacher model that encodes IPA/PanPhon phonetic features.

    Takes pre-computed phonetic feature vectors (from Epitran + PanPhon)
    and encodes them into 128-dimensional embeddings. This model only
    works for languages with Epitran support.

    Architecture:
        Input: PanPhon features [B, L, 24]
        -> BiLSTM
        -> Self-Attention + Residual
        -> Attention Pooling
        -> Projection to 128-dim
        -> L2 Normalization
    """

    def __init__(
            self,
            feature_dim: int = 24,  # PanPhon feature dimension
            hidden_dim: int = 128,
            embed_dim: int = 128,  # Output dimension (up from 64)
            num_layers: int = 2,
            num_attention_heads: int = 2,
            dropout: float = 0.2,
    ):
        super().__init__()

        self.feature_dim = feature_dim
        self.embed_dim = embed_dim

        # Input projection
        self.input_proj = nn.Linear(feature_dim, hidden_dim)

        # BiLSTM encoder
        self.bilstm = nn.LSTM(
            input_size=hidden_dim,
            hidden_size=hidden_dim,
            num_layers=num_layers,
            batch_first=True,
            bidirectional=True,
            dropout=dropout if num_layers > 1 else 0,
        )

        # Self-attention (operates on BiLSTM output which is 2*hidden_dim)
        self.self_attention = SelfAttention(
            hidden_dim=hidden_dim * 2,
            num_heads=num_attention_heads,
            dropout=dropout,
        )

        # Attention pooling
        self.pooling = AttentionPooling(
            hidden_dim=hidden_dim * 2,
            dropout=dropout,
        )

        # Output projection
        self.output_proj = nn.Sequential(
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, embed_dim),
            nn.LayerNorm(embed_dim),
        )

    def forward(
            self,
            features: torch.Tensor,
            lengths: torch.Tensor,
    ) -> torch.Tensor:
        """
        Encode phonetic features to embedding.

        Args:
            features: PanPhon features [B, L, 24]
            lengths: Sequence lengths [B] (CPU tensor)

        Returns:
            L2-normalized embeddings [B, embed_dim]
        """
        batch_size, max_len, _ = features.shape
        device = features.device

        # Project input
        x = self.input_proj(features)  # [B, L, hidden]

        # Create mask
        mask = torch.arange(max_len, device=device).unsqueeze(0) < lengths.to(device).unsqueeze(1)

        # BiLSTM
        packed = nn.utils.rnn.pack_padded_sequence(
            x, lengths.cpu(), batch_first=True, enforce_sorted=False
        )
        lstm_out, _ = self.bilstm(packed)
        lstm_out, _ = nn.utils.rnn.pad_packed_sequence(
            lstm_out, batch_first=True, total_length=max_len
        )  # [B, L, hidden*2]

        # Self-attention with residual
        attended, _ = self.self_attention(lstm_out, mask)
        attended = attended + lstm_out

        # Attention pooling
        pooled, _ = self.pooling(attended, mask)  # [B, hidden*2]

        # Project and normalize
        embedding = self.output_proj(pooled)
        embedding = F.normalize(embedding, p=2, dim=-1)

        return embedding


# ============================================================================
# Student Model: UniversalEncoder
# ============================================================================

class UniversalEncoder(nn.Module):
    """
    Student model that encodes raw characters with script/language awareness.

    This is the "universal" encoder that can handle any script:
    - Alphabetic scripts (Latin, Cyrillic, Greek, etc.) are read natively
    - CJK scripts are pre-romanized via AnyAscii
    - Korean is pre-decomposed to Jamo

    The model learns to map noisy/variant spellings to the Teacher's
    clean phonetic embeddings through distillation.

    Architecture:
        Input: Character IDs [B, L] + Script ID [B] + Language ID [B]
        -> Character Embedding + Script Embedding + Language Embedding
        -> BiLSTM
        -> Self-Attention + Residual
        -> Attention Pooling
        -> Projection to 128-dim
        -> L2 Normalization

    Training features:
        - Language dropout (50%): Replaces lang_id with <UNK> during training
        - Noise augmentation: Applied in collate function, not here

    Note: Default vocab sizes are conservative estimates. Actual sizes are
    determined by the extracted vocabulary and passed in during model creation.
    The two-pass extraction from gn/wd/tgn typically yields ~4000 char tokens
    and ~1000 languages.
    """

    def __init__(
            self,
            vocab_size: int = 5000,  # Conservative default; actual from vocab
            num_scripts: int = 25,   # 20 defined + buffer
            num_langs: int = 1200,   # Wikidata has many languages
            char_embed_dim: int = 64,
            script_embed_dim: int = 16,
            lang_embed_dim: int = 16,
            hidden_dim: int = 128,
            embed_dim: int = 128,
            num_layers: int = 2,
            num_attention_heads: int = 2,
            dropout: float = 0.2,
            lang_dropout: float = 0.5,
            num_length_buckets: int = 16,
            length_embed_dim: int = 8,
    ):
        super().__init__()

        self.embed_dim = embed_dim
        self.lang_dropout_rate = lang_dropout

        # Embeddings
        self.char_embed = nn.Embedding(vocab_size, char_embed_dim, padding_idx=0)
        self.script_embed = nn.Embedding(num_scripts, script_embed_dim)
        self.lang_embed = nn.Embedding(num_langs, lang_embed_dim, padding_idx=0)  # 0 = <UNK>

        # Length bucket embedding: 16 buckets, bucket 0=(1-2), 1=(3-4), ..., 15=(31+)
        self.num_length_buckets = num_length_buckets
        self.length_embed = nn.Embedding(num_length_buckets, length_embed_dim)

        # Combined input dimension
        input_dim = char_embed_dim + script_embed_dim + lang_embed_dim + length_embed_dim

        # Input projection to hidden_dim
        self.input_proj = nn.Linear(input_dim, hidden_dim)
        self.input_norm = nn.LayerNorm(hidden_dim)

        # BiLSTM encoder
        self.bilstm = nn.LSTM(
            input_size=hidden_dim,
            hidden_size=hidden_dim,
            num_layers=num_layers,
            batch_first=True,
            bidirectional=True,
            dropout=dropout if num_layers > 1 else 0,
        )

        # Self-attention
        self.self_attention = SelfAttention(
            hidden_dim=hidden_dim * 2,
            num_heads=num_attention_heads,
            dropout=dropout,
        )

        # Attention pooling
        self.pooling = AttentionPooling(
            hidden_dim=hidden_dim * 2,
            dropout=dropout,
        )

        # Output projection
        self.output_proj = nn.Sequential(
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, embed_dim),
            nn.LayerNorm(embed_dim),
        )

    def _length_bucket(self, lengths: torch.Tensor) -> torch.Tensor:
        """Map raw sequence lengths to bucket indices.

        Buckets: 0=(1-2), 1=(3-4), ..., 14=(29-30), 15=(31+)
        Using floor((length - 1) / 2), clamped to [0, num_buckets - 1].
        """
        buckets = (lengths.to(torch.long) - 1) // 2
        return buckets.clamp(0, self.num_length_buckets - 1)

    def forward(
            self,
            char_ids: torch.Tensor,
            script_ids: torch.Tensor,
            lang_ids: torch.Tensor,
            lengths: torch.Tensor,
    ) -> torch.Tensor:
        """
        Encode characters to embedding.

        Args:
            char_ids: Character token IDs [B, L]
            script_ids: Script type IDs [B]
            lang_ids: Language IDs [B]
            lengths: Sequence lengths [B] (CPU tensor)

        Returns:
            L2-normalized embeddings [B, embed_dim]
        """
        batch_size, max_len = char_ids.shape
        device = char_ids.device

        # Apply language dropout during training
        if self.training and self.lang_dropout_rate > 0:
            dropout_mask = torch.rand(batch_size, device=device) < self.lang_dropout_rate
            lang_ids = lang_ids.clone()
            lang_ids[dropout_mask] = 0  # 0 = <UNK>

        # Character embeddings [B, L, char_dim]
        c_emb = self.char_embed(char_ids)

        # Script embeddings broadcast to sequence [B, L, script_dim]
        s_emb = self.script_embed(script_ids)
        s_emb = s_emb.unsqueeze(1).expand(-1, max_len, -1)

        # Language embeddings broadcast to sequence [B, L, lang_dim]
        l_emb = self.lang_embed(lang_ids)
        l_emb = l_emb.unsqueeze(1).expand(-1, max_len, -1)

        # Length bucket embeddings broadcast to sequence [B, L, length_dim]
        length_buckets = self._length_bucket(lengths.to(device))
        lb_emb = self.length_embed(length_buckets)            # [B, length_dim]
        lb_emb = lb_emb.unsqueeze(1).expand(-1, max_len, -1)  # [B, L, length_dim]

        # Concatenate all embeddings
        combined = torch.cat([c_emb, s_emb, l_emb, lb_emb], dim=-1)  # [B, L, input_dim]

        # Project to hidden dim
        x = self.input_proj(combined)  # [B, L, hidden]
        x = self.input_norm(x)

        # Create mask
        mask = torch.arange(max_len, device=device).unsqueeze(0) < lengths.to(device).unsqueeze(1)

        # BiLSTM
        packed = nn.utils.rnn.pack_padded_sequence(
            x, lengths.cpu(), batch_first=True, enforce_sorted=False
        )
        lstm_out, _ = self.bilstm(packed)
        lstm_out, _ = nn.utils.rnn.pad_packed_sequence(
            lstm_out, batch_first=True, total_length=max_len
        )

        # Self-attention with residual
        attended, _ = self.self_attention(lstm_out, mask)
        attended = attended + lstm_out

        # Attention pooling
        pooled, _ = self.pooling(attended, mask)

        # Project and normalize
        embedding = self.output_proj(pooled)
        embedding = F.normalize(embedding, p=2, dim=-1)

        return embedding


# ============================================================================
# Hybrid Model for Inference
# ============================================================================

class HybridPhoneticModel(nn.Module):
    """
    Combined model that uses Teacher for supported languages and Student for others.

    At inference time:
    - If language has Epitran support AND we can compute IPA: use Teacher
    - Otherwise: use Student

    The gating is determined by input availability, not learned.

    For training, Teacher and Student are trained separately in different phases.
    This class is primarily for inference convenience.
    """

    def __init__(
            self,
            teacher: PhoneticEncoder,
            student: UniversalEncoder,
    ):
        super().__init__()
        self.teacher = teacher
        self.student = student

        # Verify embedding dimensions match
        assert teacher.embed_dim == student.embed_dim, \
            f"Embedding dimension mismatch: teacher={teacher.embed_dim}, student={student.embed_dim}"

        self.embed_dim = teacher.embed_dim

    def forward(
            self,
            # Student inputs (always required)
            char_ids: torch.Tensor,
            script_ids: torch.Tensor,
            lang_ids: torch.Tensor,
            char_lengths: torch.Tensor,
            # Teacher inputs (optional)
            features: Optional[torch.Tensor] = None,
            feature_lengths: Optional[torch.Tensor] = None,
            use_teacher_mask: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        """
        Compute embeddings using appropriate encoder per sample.

        Args:
            char_ids: Character IDs for Student [B, L_char]
            script_ids: Script IDs [B]
            lang_ids: Language IDs [B]
            char_lengths: Character sequence lengths [B]
            features: PanPhon features for Teacher [B, L_feat, 24] (optional)
            feature_lengths: Feature sequence lengths [B] (optional)
            use_teacher_mask: Boolean mask indicating which samples use Teacher [B]

        Returns:
            Embeddings [B, embed_dim]
        """
        batch_size = char_ids.shape[0]
        device = char_ids.device

        # If no Teacher inputs, use Student for everything
        if features is None or use_teacher_mask is None:
            return self.student(char_ids, script_ids, lang_ids, char_lengths)

        # Initialize output
        embeddings = torch.zeros(batch_size, self.embed_dim, device=device)

        # Process Teacher samples
        teacher_indices = use_teacher_mask.nonzero(as_tuple=True)[0]
        if len(teacher_indices) > 0:
            teacher_embeddings = self.teacher(
                features[teacher_indices],
                feature_lengths[teacher_indices],
            )
            embeddings[teacher_indices] = teacher_embeddings

        # Process Student samples
        student_indices = (~use_teacher_mask).nonzero(as_tuple=True)[0]
        if len(student_indices) > 0:
            student_embeddings = self.student(
                char_ids[student_indices],
                script_ids[student_indices],
                lang_ids[student_indices],
                char_lengths[student_indices],
            )
            embeddings[student_indices] = student_embeddings

        return embeddings

    def encode_text(
            self,
            char_ids: torch.Tensor,
            script_ids: torch.Tensor,
            lang_ids: torch.Tensor,
            char_lengths: torch.Tensor,
    ) -> torch.Tensor:
        """
        Convenience method to encode using Student only.

        This is the primary inference method when IPA features
        are not pre-computed.
        """
        return self.student(char_ids, script_ids, lang_ids, char_lengths)


# ============================================================================
# Loss Functions
# ============================================================================

class TripletMarginLossWithMining(nn.Module):
    """
    Triplet margin loss with optional online hard negative mining.

    L = max(0, d(a, p) - d(a, n) + margin)

    where d is Euclidean distance (L2).
    """

    def __init__(
            self,
            margin: float = 0.3,
            mining: str = 'none',  # 'none', 'hard', 'semi-hard'
    ):
        super().__init__()
        self.margin = margin
        self.mining = mining

    def forward(
            self,
            anchor: torch.Tensor,
            positive: torch.Tensor,
            negative: torch.Tensor,
    ) -> torch.Tensor:
        """
        Compute triplet loss.

        Args:
            anchor: Anchor embeddings [B, D]
            positive: Positive embeddings [B, D]
            negative: Negative embeddings [B, D]

        Returns:
            Scalar loss
        """
        # L2 distances
        d_ap = F.pairwise_distance(anchor, positive)
        d_an = F.pairwise_distance(anchor, negative)

        # Triplet loss
        losses = F.relu(d_ap - d_an + self.margin)

        return losses.mean()


class ContrastiveDistillationLoss(nn.Module):
    """
    Loss for Phase 2: Aligning Student to Teacher.

    Combines:
    1. MSE loss between Student and Teacher embeddings
    2. Cosine similarity loss
    3. Optional contrastive component

    The Student learns to map noisy inputs to the Teacher's
    clean phonetic representations.
    """

    def __init__(
            self,
            mse_weight: float = 1.0,
            cosine_weight: float = 1.0,
    ):
        super().__init__()
        self.mse_weight = mse_weight
        self.cosine_weight = cosine_weight
        self.mse = nn.MSELoss()
        self.cosine = nn.CosineEmbeddingLoss()

    def forward(
            self,
            student_emb: torch.Tensor,
            teacher_emb: torch.Tensor,
    ) -> Tuple[torch.Tensor, Dict[str, float]]:
        """
        Compute distillation loss.

        Args:
            student_emb: Student embeddings [B, D]
            teacher_emb: Teacher embeddings [B, D] (detached, no gradient)

        Returns:
            Tuple of (loss, metrics_dict)
        """
        # Ensure Teacher gradients are stopped
        teacher_emb = teacher_emb.detach()

        # MSE loss
        mse_loss = self.mse(student_emb, teacher_emb)

        # Cosine similarity loss (target = 1 for same pair)
        targets = torch.ones(student_emb.shape[0], device=student_emb.device)
        cosine_loss = self.cosine(student_emb, teacher_emb, targets)

        # Combined loss
        total_loss = self.mse_weight * mse_loss + self.cosine_weight * cosine_loss

        # Metrics
        with torch.no_grad():
            cosine_sim = F.cosine_similarity(student_emb, teacher_emb).mean()

        metrics = {
            'mse_loss': mse_loss.item(),
            'cosine_loss': cosine_loss.item(),
            'cosine_sim': cosine_sim.item(),
        }

        return total_loss, metrics


# ============================================================================
# Model Factory
# ============================================================================

def create_teacher(
        feature_dim: int = 24,
        hidden_dim: int = 128,
        embed_dim: int = 128,
        num_layers: int = 2,
        dropout: float = 0.2,
) -> PhoneticEncoder:
    """Create a Teacher model with default configuration."""
    return PhoneticEncoder(
        feature_dim=feature_dim,
        hidden_dim=hidden_dim,
        embed_dim=embed_dim,
        num_layers=num_layers,
        dropout=dropout,
    )


def create_student(
        vocab_size: int = 1200,
        num_scripts: int = 20,
        num_langs: int = 300,
        char_embed_dim: int = 64,
        script_embed_dim: int = 16,
        lang_embed_dim: int = 16,
        hidden_dim: int = 128,
        embed_dim: int = 128,
        num_layers: int = 2,
        dropout: float = 0.2,
        lang_dropout: float = 0.5,
        num_length_buckets: int = 16,
        length_embed_dim: int = 8,
) -> UniversalEncoder:
    """Create a Student model with default configuration."""
    return UniversalEncoder(
        vocab_size=vocab_size,
        num_scripts=num_scripts,
        num_langs=num_langs,
        char_embed_dim=char_embed_dim,
        script_embed_dim=script_embed_dim,
        lang_embed_dim=lang_embed_dim,
        hidden_dim=hidden_dim,
        embed_dim=embed_dim,
        num_layers=num_layers,
        dropout=dropout,
        lang_dropout=lang_dropout,
        num_length_buckets=num_length_buckets,
        length_embed_dim=length_embed_dim,
    )


def create_hybrid(
        teacher: PhoneticEncoder,
        student: UniversalEncoder,
) -> HybridPhoneticModel:
    """Create a Hybrid model from Teacher and Student."""
    return HybridPhoneticModel(teacher=teacher, student=student)


def load_checkpoint(
        path: str,
        model: nn.Module,
        optimizer: Optional[torch.optim.Optimizer] = None,
        device: str = 'cpu',
) -> Dict[str, Any]:
    """
    Load a model checkpoint.

    Args:
        path: Checkpoint file path
        model: Model to load weights into
        optimizer: Optional optimizer to load state into
        device: Device to load to

    Returns:
        Checkpoint metadata dict
    """
    checkpoint = torch.load(path, map_location=device)

    model.load_state_dict(checkpoint['model_state_dict'])

    if optimizer is not None and 'optimizer_state_dict' in checkpoint:
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])

    return {
        'epoch': checkpoint.get('epoch', 0),
        'step': checkpoint.get('step', 0),
        'best_loss': checkpoint.get('best_loss', float('inf')),
        'config': checkpoint.get('config', {}),
    }


def save_checkpoint(
        path: str,
        model: nn.Module,
        optimizer: torch.optim.Optimizer,
        epoch: int,
        step: int,
        best_loss: float,
        config: Dict[str, Any],
):
    """
    Save a model checkpoint.

    Args:
        path: Output file path
        model: Model to save
        optimizer: Optimizer to save
        epoch: Current epoch
        step: Current step
        best_loss: Best validation loss
        config: Model configuration
    """
    torch.save({
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
        'epoch': epoch,
        'step': step,
        'best_loss': best_loss,
        'config': config,
    }, path)


# ============================================================================
# Testing
# ============================================================================

if __name__ == '__main__':
    # Test model creation and forward pass
    print("Testing PhoneticEncoder (Teacher)...")
    teacher = create_teacher()
    print(f"  Parameters: {sum(p.numel() for p in teacher.parameters()):,}")

    # Dummy input
    features = torch.randn(4, 10, 24)  # [B, L, features]
    lengths = torch.tensor([10, 8, 6, 4])

    teacher.eval()
    with torch.no_grad():
        emb = teacher(features, lengths)
    print(f"  Output shape: {emb.shape}")
    print(f"  Output norm: {emb.norm(dim=-1)}")  # Should be ~1.0

    print("\nTesting UniversalEncoder (Student)...")
    student = create_student()
    print(f"  Parameters: {sum(p.numel() for p in student.parameters()):,}")

    # Dummy input
    char_ids = torch.randint(1, 100, (4, 15))  # [B, L]
    script_ids = torch.tensor([0, 1, 2, 0])  # [B]
    lang_ids = torch.tensor([1, 2, 3, 0])  # [B]
    char_lengths = torch.tensor([15, 12, 10, 8])

    student.eval()
    with torch.no_grad():
        emb = student(char_ids, script_ids, lang_ids, char_lengths)
    print(f"  Output shape: {emb.shape}")
    print(f"  Output norm: {emb.norm(dim=-1)}")

    print("\nTesting HybridPhoneticModel...")
    hybrid = create_hybrid(teacher, student)
    print(f"  Total parameters: {sum(p.numel() for p in hybrid.parameters()):,}")

    # Test Student-only path
    with torch.no_grad():
        emb = hybrid.encode_text(char_ids, script_ids, lang_ids, char_lengths)
    print(f"  Student-only output shape: {emb.shape}")

    print("\nTesting loss functions...")

    # Triplet loss
    triplet_loss = TripletMarginLossWithMining(margin=0.3)
    anchor = F.normalize(torch.randn(8, 128), dim=-1)
    positive = F.normalize(torch.randn(8, 128), dim=-1)
    negative = F.normalize(torch.randn(8, 128), dim=-1)
    loss = triplet_loss(anchor, positive, negative)
    print(f"  Triplet loss: {loss.item():.4f}")

    # Distillation loss
    distill_loss = ContrastiveDistillationLoss()
    student_emb = F.normalize(torch.randn(8, 128), dim=-1)
    teacher_emb = F.normalize(torch.randn(8, 128), dim=-1)
    loss, metrics = distill_loss(student_emb, teacher_emb)
    print(f"  Distillation loss: {loss.item():.4f}")
    print(f"  Metrics: {metrics}")

    print("\nAll tests passed!")