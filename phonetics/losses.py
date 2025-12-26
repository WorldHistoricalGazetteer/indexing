"""
Loss Functions for Phonetic Similarity Model Training.

Provides:
- TripletLoss: For contrastive learning (Phase 1 and Phase 3)
- RobustAlignmentLoss: For Student-Teacher alignment (Phase 2)
"""

import torch
import torch.nn as nn
import torch.nn.functional as F

from .config import Config


class TripletLoss(nn.Module):
    """
    Standard triplet loss with margin.
    
    Pulls anchor closer to positive, pushes it away from negative.
    Used in Phase 1 (Teacher) and Phase 3 (Student fine-tuning).
    """

    def __init__(self, margin: float = Config.TRIPLET_MARGIN):
        super().__init__()
        self.margin = margin

    def forward(
        self,
        anchor: torch.Tensor,
        positive: torch.Tensor,
        negative: torch.Tensor
    ) -> torch.Tensor:
        """
        Compute triplet loss.
        
        Args:
            anchor: (batch, embed_dim)
            positive: (batch, embed_dim)
            negative: (batch, embed_dim)
        
        Returns:
            Scalar loss
        """
        pos_dist = (anchor - positive).pow(2).sum(dim=-1)
        neg_dist = (anchor - negative).pow(2).sum(dim=-1)
        loss = F.relu(pos_dist - neg_dist + self.margin)
        return loss.mean()


class RobustAlignmentLoss(nn.Module):
    """
    Combined MSE and Cosine loss for Student-Teacher alignment.
    
    - MSE fixes position in embedding space
    - Cosine fixes orientation (angular alignment)
    
    Used in Phase 2 to align Student (char encoder) to Teacher (phonetic encoder).
    """

    def __init__(self, cosine_weight: float = Config.ALIGNMENT_COSINE_WEIGHT):
        super().__init__()
        self.cosine_weight = cosine_weight

    def forward(
        self,
        char_emb: torch.Tensor,
        phone_emb: torch.Tensor
    ) -> torch.Tensor:
        """
        Compute alignment loss.
        
        Args:
            char_emb: (batch, embed_dim) - Student output
            phone_emb: (batch, embed_dim) - Teacher output (target)
        
        Returns:
            Scalar loss
        """
        # MSE loss (Euclidean distance proxy)
        mse = F.mse_loss(char_emb, phone_emb)
        
        # Cosine distance (orientation proxy)
        cosine_dist = 1.0 - F.cosine_similarity(char_emb, phone_emb).mean()
        
        return mse + (self.cosine_weight * cosine_dist)
