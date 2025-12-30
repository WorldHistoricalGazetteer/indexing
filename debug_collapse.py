import torch
import torch.nn.functional as F
from pathlib import Path
import sys

# Paths to your models
MODEL_PATHS = {
    "Phase 2 (Pre-train)": "/ix1/whcdh/models/phonetic/checkpoints/phase2.pt",
    "Phase 3A (Metric)": "/ix1/whcdh/models/phonetic/checkpoints/phase3_a.pt"
}

# Test inputs: Distinct names that should NOT match
PAIRS = [
    ("London", "en", "Paris", "en"),  # Distinct English
    ("London", "en", "Beijing", "en"),  # Very distinct English
    ("Yaqut", "ar", "Kima", "he"),  # The Benchmark case (Target Match)
    ("Yaqut", "ar", "NotYaqut", "ar"),  # Distinct Arabic
    ("Kima", "he", "NotKima", "he"),  # Distinct Hebrew
    ("RandomA", "en", "RandomB", "en"),  # Random noise
]


def load_model_components(model_path):
    checkpoint = torch.load(model_path, map_location='cpu')

    # Dynamically import your architecture
    # Assuming the script is running from repo root
    from phonetics.models import HybridPhoneticModel, PhoneticEncoder, CharEncoder
    from phonetics.vocab import CharVocab, LangVocab

    vocab_dir = Path(model_path).parent
    base = Path(model_path).stem.replace('_a', '')  # handle phase3_a naming
    if 'phase2' in base: base = 'phase2'

    char_vocab = CharVocab.load(vocab_dir / f'{base}_char_vocab.pkl')
    lang_vocab = LangVocab.load(vocab_dir / f'{base}_lang_vocab.pkl')

    encoder = PhoneticEncoder()
    char_enc = CharEncoder(checkpoint['char_vocab_size'], checkpoint['num_langs'])
    model = HybridPhoneticModel(encoder, char_enc)
    model.load_state_dict(checkpoint['model_state'])
    model.eval()

    return model, char_vocab, lang_vocab


def get_embedding(model, char_vocab, lang_vocab, text, lang_code):
    # Manual encoding to ensure no benchmark script bugs interfere
    chars = char_vocab.encode(text.lower())
    lang_id = lang_vocab.encode(lang_code)

    c_tensor = torch.tensor([chars], dtype=torch.long)
    l_tensor = torch.tensor([lang_id], dtype=torch.long)
    len_tensor = torch.tensor([len(chars)], dtype=torch.long)

    with torch.no_grad():
        emb = model.encode_char_only(c_tensor, l_tensor, len_tensor)
    return emb[0]


def run_diagnostics():
    print("=" * 80)
    print("MODEL COLLAPSE DIAGNOSTIC")
    print("=" * 80)

    for name, path in MODEL_PATHS.items():
        print(f"\nChecking: {name}")
        print(f"Path: {path}")

        if not Path(path).exists():
            print("  [!] Model file not found.")
            continue

        try:
            model, c_vocab, l_vocab = load_model_components(path)
        except Exception as e:
            print(f"  [!] Failed to load: {e}")
            continue

        print("-" * 60)
        print(f"{'Pair':<40} | {'Cosine Sim':<10} | {'Euclidean':<10}")
        print("-" * 60)

        vectors = []

        for t1, l1, t2, l2 in PAIRS:
            v1 = get_embedding(model, c_vocab, l_vocab, t1, l1)
            v2 = get_embedding(model, c_vocab, l_vocab, t2, l2)

            sim = F.cosine_similarity(v1.unsqueeze(0), v2.unsqueeze(0)).item()
            dist = torch.dist(v1, v2).item()

            label = f"{t1}({l1}) vs {t2}({l2})"
            print(f"{label:<40} | {sim:<10.4f} | {dist:<10.4f}")

            vectors.append(v1)
            vectors.append(v2)

        # Check Variance Statistics
        all_vecs = torch.stack(vectors)
        mean_norm = all_vecs.norm(dim=1).mean().item()
        std_dev = all_vecs.std(dim=0).mean().item()  # Mean std dev across dimensions

        print("-" * 60)
        print("VECTOR STATISTICS:")
        print(f"  Mean Vector Norm: {mean_norm:.4f}")
        print(f"  Mean Dim StdDev:  {std_dev:.4f} (If approx 0.0, model is collapsed)")

        # Check if vectors are identical
        if std_dev < 1e-3:
            print("  Result: COMPLETE COLLAPSE (Constant Output)")
        elif std_dev < 0.05:
            print("  Result: PARTIAL COLLAPSE (Low Variance)")
        else:
            print("  Result: Healthy Variance")


if __name__ == "__main__":
    run_diagnostics()