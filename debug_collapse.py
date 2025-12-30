import torch
import torch.nn.functional as F
from pathlib import Path
import sys
import glob

# Paths to your models
MODEL_PATHS = {
    "Phase 2 (Pre-train)": "/ix1/whcdh/models/phonetic/checkpoints/phase2.pt",
    "Phase 3A (Metric)": "/ix1/whcdh/models/phonetic/checkpoints/phase3_a.pt"
}

# Test inputs
PAIRS = [
    ("London", "en", "Paris", "en"),  # Distinct English
    ("London", "en", "Beijing", "en"),  # Very distinct English
    ("Yaqut", "ar", "Kima", "he"),  # Benchmark Target Match
    ("Yaqut", "ar", "NotYaqut", "ar"),  # Distinct Arabic
    ("Kima", "he", "NotKima", "he"),  # Distinct Hebrew
    ("RandomA", "en", "RandomB", "en"),  # Random noise
]


def find_vocab_file(directory, suffix):
    """Robustly find a vocab file ending with suffix (e.g., '_char_vocab.pkl')."""
    # Try exact match first
    # Then try phase2/phase3 variants
    # Then try any matching file
    candidates = [
        f"phase3_a{suffix}",
        f"phase3{suffix}",
        f"phase2{suffix}",
        f"vocab{suffix}",
        f"*{suffix}"
    ]

    for pat in candidates:
        matches = list(Path(directory).glob(pat))
        if matches:
            return matches[0]
    return None


def load_model_components(model_path):
    print(f"  Loading checkpoint: {Path(model_path).name}")
    checkpoint = torch.load(model_path, map_location='cpu')

    # Imports
    from phonetics.models import HybridPhoneticModel, PhoneticEncoder, CharEncoder
    from phonetics.vocab import CharVocab, LangVocab

    vocab_dir = Path(model_path).parent

    # 1. ROBUST VOCAB LOADING
    char_vocab_path = find_vocab_file(vocab_dir, "_char_vocab.pkl")
    lang_vocab_path = find_vocab_file(vocab_dir, "_lang_vocab.pkl")

    if not char_vocab_path or not lang_vocab_path:
        raise FileNotFoundError(f"Could not find vocab files in {vocab_dir}")

    print(f"  Using vocab: {char_vocab_path.name}")
    char_vocab = CharVocab.load(char_vocab_path)
    lang_vocab = LangVocab.load(lang_vocab_path)

    # 2. ROBUST DIMENSION DETECTION
    state_dict = checkpoint.get('model_state', checkpoint)

    # Infer sizes from weights if config keys are missing
    if 'char_encoder.embedding.weight' in state_dict:
        char_vocab_size = state_dict['char_encoder.embedding.weight'].shape[0]
    else:
        char_vocab_size = checkpoint.get('char_vocab_size', 1000)  # Fallback

    if 'char_encoder.lang_embedding.weight' in state_dict:
        num_langs = state_dict['char_encoder.lang_embedding.weight'].shape[0]
    else:
        num_langs = checkpoint.get('num_langs', 100)  # Fallback

    print(f"  Detected dimensions: Chars={char_vocab_size}, Langs={num_langs}")

    # 3. BUILD MODEL
    encoder = PhoneticEncoder()
    char_enc = CharEncoder(char_vocab_size, num_langs)
    model = HybridPhoneticModel(encoder, char_enc)
    model.load_state_dict(state_dict)
    model.eval()

    return model, char_vocab, lang_vocab


def get_embedding(model, char_vocab, lang_vocab, text, lang_code):
    # Handle missing UNK_LANG by defaulting to 'en' or index 0
    try:
        lang_id = lang_vocab.encode(lang_code)
    except:
        lang_id = 0

    chars = char_vocab.encode(text.lower())

    c_tensor = torch.tensor([chars], dtype=torch.long)
    l_tensor = torch.tensor([lang_id], dtype=torch.long)
    # FIX: Explicitly use CPU length tensor to avoid pack_padded errors
    len_tensor = torch.tensor([len(chars)], dtype=torch.long)

    with torch.no_grad():
        emb = model.encode_char_only(c_tensor, l_tensor, len_tensor)
    return emb[0]


def run_diagnostics():
    print("=" * 80)
    print("MODEL COLLAPSE DIAGNOSTIC (ROBUST MODE)")
    print("=" * 80)

    for name, path in MODEL_PATHS.items():
        print(f"\nChecking: {name}")

        if not Path(path).exists():
            print("  [!] Model file not found.")
            continue

        try:
            model, c_vocab, l_vocab = load_model_components(path)
        except Exception as e:
            print(f"  [!] Failed to load: {e}")
            import traceback
            traceback.print_exc()
            continue

        print("-" * 60)
        print(f"{'Pair':<40} | {'Cos Sim':<8} | {'Dist':<8} | {'Norms'}")
        print("-" * 60)

        vectors = []

        for t1, l1, t2, l2 in PAIRS:
            v1 = get_embedding(model, c_vocab, l_vocab, t1, l1)
            v2 = get_embedding(model, c_vocab, l_vocab, t2, l2)

            sim = F.cosine_similarity(v1.unsqueeze(0), v2.unsqueeze(0)).item()
            dist = torch.dist(v1, v2).item()
            n1, n2 = v1.norm().item(), v2.norm().item()

            label = f"{t1} vs {t2}"
            print(f"{label:<40} | {sim:<8.4f} | {dist:<8.4f} | {n1:.2f}, {n2:.2f}")

            vectors.append(v1)
            vectors.append(v2)

        # Check Variance Statistics
        all_vecs = torch.stack(vectors)
        mean_norm = all_vecs.norm(dim=1).mean().item()
        std_dev = all_vecs.std(dim=0).mean().item()

        print("-" * 60)
        print(f"  Mean Norm: {mean_norm:.4f}")
        print(f"  Variance (StdDev): {std_dev:.4f}")

        if std_dev < 1e-3:
            print("  STATUS: COMPLETE COLLAPSE (Model is dead)")
        elif std_dev < 0.05:
            print("  STATUS: PARTIAL COLLAPSE (Low variance)")
        else:
            print("  STATUS: HEALTHY")


if __name__ == "__main__":
    run_diagnostics()