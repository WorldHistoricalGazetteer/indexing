"""CRNN (CNN + BiLSTM + CTC) recogniser for B' — trained on REAL (crop, transcript) pairs so its
encoder is a REAL-domain feature extractor (no synthetic domain gap). Exposes:
  forward(x) -> CTC log-probs (T,B,C)
  encode(x)  -> (seq_feats (B,T,512), pooled (B,512))  # pooled = style embedding; seq = per-glyph
Input: 1 x 32 x W grayscale (1 = paper). Height collapses to 1; T ~ W/4.
"""
import torch, torch.nn as nn, torch.nn.functional as Fn

class CRNN(nn.Module):
    def __init__(self, n_class, emb=512):
        super().__init__()
        def c(i, o, k=3, s=1, p=1, bn=False):
            layers = [nn.Conv2d(i, o, k, s, p)]
            if bn: layers.append(nn.BatchNorm2d(o))
            layers.append(nn.ReLU(True))
            return layers
        self.cnn = nn.Sequential(
            *c(1, 64), nn.MaxPool2d(2, 2),                          # 64 x16 xW/2
            *c(64, 128), nn.MaxPool2d(2, 2),                        # 128 x8 xW/4
            *c(128, 256), *c(256, 256), nn.MaxPool2d((2, 2), (2, 1), (0, 1)),   # 256 x4 xW/4
            *c(256, 512, bn=True), *c(512, 512, bn=True), nn.MaxPool2d((2, 2), (2, 1), (0, 1)),  # 512 x2 xW/4
            *c(512, 512, k=2, p=0, bn=True),                        # 512 x1 x(W/4-1)
        )
        self.rnn = nn.LSTM(512, 256, num_layers=2, bidirectional=True, batch_first=True)
        self.fc = nn.Linear(512, n_class)

    def _seq(self, x):
        f = self.cnn(x)                    # (B,512,1,T)
        assert f.size(2) == 1, f.shape
        f = f.squeeze(2).permute(0, 2, 1)  # (B,T,512)
        h, _ = self.rnn(f)                 # (B,T,512)
        return h

    def forward(self, x):
        h = self._seq(x)
        return Fn.log_softmax(self.fc(h), dim=2).permute(1, 0, 2)   # (T,B,C) for CTCLoss

    @torch.no_grad()
    def encode(self, x):
        h = self._seq(x)                   # (B,T,512)
        pooled = Fn.normalize(h.mean(1), dim=1)   # style embedding (B,512)
        return h, pooled

    @torch.no_grad()
    def per_glyph(self, x):
        """per-character embeddings via CTC argmax alignment (collapse repeats+blanks). blank=0."""
        h = self._seq(x)                   # (B,T,512)
        logits = self.fc(h)                # (B,T,C)
        arg = logits.argmax(2)             # (B,T)
        out = []
        for b in range(x.size(0)):
            cols, prev = [], -1
            for t in range(arg.size(1)):
                a = int(arg[b, t])
                if a != 0 and a != prev:
                    cols.append((t, a))
                prev = a
            out.append([(a, Fn.normalize(h[b, t], dim=0).cpu().numpy()) for t, a in cols])
        return out
