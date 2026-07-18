"""Small CNN style-encoder + supervised-contrastive loss.

Variable-width labels are standardised to 64x192 upstream; the net is a compact conv stack
-> global average pool -> 128-d L2-normalised embedding. Trained so same-FONT crops (different
words) are neighbours, i.e. the embedding encodes the typographic 'hand', not the characters.
"""
import torch, torch.nn as nn, torch.nn.functional as Fn

class StyleEncoder(nn.Module):
    def __init__(self, dim=128):
        super().__init__()
        def blk(i, o, s):
            return nn.Sequential(nn.Conv2d(i, o, 3, 1, 1), nn.BatchNorm2d(o), nn.ReLU(True),
                                 nn.Conv2d(o, o, 3, s, 1), nn.BatchNorm2d(o), nn.ReLU(True))
        self.body = nn.Sequential(
            blk(1, 32, 2), blk(32, 64, 2), blk(64, 128, 2), blk(128, 256, 2))
        self.pool = nn.AdaptiveAvgPool2d(1)
        self.head = nn.Linear(256, dim)

    def forward(self, x):
        h = self.pool(self.body(x)).flatten(1)
        return Fn.normalize(self.head(h), dim=1)

def supcon_loss(z, y, temp=0.1):
    """Supervised contrastive loss (Khosla et al. 2020). z: (N,d) L2-normed; y: (N,)."""
    N = z.shape[0]
    sim = z @ z.t() / temp
    sim = sim - sim.max(dim=1, keepdim=True).values.detach()
    eye = torch.eye(N, device=z.device, dtype=torch.bool)
    same = (y[:, None] == y[None, :]) & ~eye
    exp = torch.exp(sim) * (~eye)
    logp = sim - torch.log(exp.sum(1, keepdim=True) + 1e-9)
    denom = same.sum(1).clamp(min=1)
    return -((logp * same).sum(1) / denom).mean()

def nt_xent(z1, z2, temp=0.2):
    """SimCLR NT-Xent: two L2-normed views (N,d) of the same N items; positives are v1_i<->v2_i.
    Unsupervised — used on REAL crops to shape the encoder's real-feature manifold (lever c)."""
    N = z1.shape[0]
    z = torch.cat([z1, z2], 0)                       # (2N,d)
    sim = z @ z.t() / temp
    sim.fill_diagonal_(-1e9)
    pos = torch.cat([torch.arange(N, 2 * N), torch.arange(0, N)]).to(z.device)
    return Fn.cross_entropy(sim, pos)
