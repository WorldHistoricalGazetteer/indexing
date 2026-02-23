"""
Convert Symphonym v7 checkpoint to safetensors format.

Run this once from the project root (where the zenodo/ folder is):

    python hf/convert_to_safetensors.py

Produces hf/model.safetensors suitable for HuggingFace upload.
The original .pt file is left untouched.
"""

import sys
from pathlib import Path

import torch
from safetensors.torch import save_file

ROOT    = Path(__file__).parent.parent
PT_PATH = ROOT / "zenodo" / "models" / "final_model.pt"
OUT_PATH = Path(__file__).parent / "model.safetensors"

print(f"Loading checkpoint from: {PT_PATH}")
ckpt = torch.load(str(PT_PATH), map_location="cpu", weights_only=False)

if not isinstance(ckpt, dict):
    print("ERROR: checkpoint is not a dict. Inspect it with torch.load() first.")
    sys.exit(1)

# Print stored metadata if present
if "config" in ckpt:
    cfg = ckpt["config"]
    print(f"  Stored config: embed_dim={cfg.get('embed_dim')}, hidden_dim={cfg.get('hidden_dim')}, "
          f"lr={cfg.get('learning_rate')}, margin={cfg.get('triplet_margin')}")
if "epoch" in ckpt:
    print(f"  Epoch: {ckpt['epoch']},  best_loss: {ckpt.get('best_loss', 'n/a'):.6f}")

# Extract the model state dict
state = (
    ckpt.get("model_state_dict")
    or ckpt.get("model_state")
    or ckpt.get("state_dict")
    or ckpt
)

print(f"  State dict: {len(state)} tensors")

# Check for hybrid-model prefix (char_encoder.* = Student weights)
student_prefix = "char_encoder."
has_prefix = any(k.startswith(student_prefix) for k in state)

if has_prefix:
    state = {k[len(student_prefix):]: v for k, v in state.items()
             if k.startswith(student_prefix)}
    print(f"  Extracted Student weights from hybrid checkpoint: {len(state)} tensors")
else:
    # Checkpoint is already Student-only (confirmed structure for final_model.pt)
    print(f"  Checkpoint is Student-only (UniversalEncoder) — no stripping needed.")

# Safetensors requires contiguous float tensors
state = {k: v.contiguous().float() for k, v in state.items()}

print(f"\nSaving to: {OUT_PATH}")
save_file(state, str(OUT_PATH))
print(f"Done.  File size: {OUT_PATH.stat().st_size / 1e6:.1f} MB")


