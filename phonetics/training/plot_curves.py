# phonetics/training/plot_curves.py
"""
Generate training curves for the paper.

This script reads the metrics JSON files saved during training and
generates publication-quality figures for the LaTeX document.

Output:
    - training_curves.pdf: Combined figure with all phases
    - phase1_curves.pdf: Phase 1 (Teacher) loss curves
    - phase2_curves.pdf: Phase 2 (Student alignment) with cosine similarity
    - phase3_curves.pdf: Phase 3 (Fine-tuning) loss curves

Usage:
    python -m phonetics.training.plot_curves \
        --metrics-dir /home/stephen/PycharmProjects/indexing/training/metrics \
        --output-dir /home/stephen/PycharmProjects/indexing/article/figures
"""

import argparse
import json
from pathlib import Path
from typing import Dict, List, Optional

try:
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False
    print("Warning: matplotlib not available. Install with: pip install matplotlib")


# Publication-quality settings
FIGSIZE_SINGLE = (6, 4)
FIGSIZE_DOUBLE = (12, 4)
FIGSIZE_TRIPLE = (14, 4)
DPI = 300
FONT_SIZE = 10

# Color scheme
COLORS = {
    'train': '#1f77b4',  # Blue
    'val': '#ff7f0e',    # Orange
    'cosine_sim': '#2ca02c',  # Green
    'lr': '#d62728',     # Red
}


def setup_style():
    """Set up publication-quality matplotlib style."""
    plt.rcParams.update({
        'font.size': FONT_SIZE,
        'axes.titlesize': FONT_SIZE + 2,
        'axes.labelsize': FONT_SIZE,
        'xtick.labelsize': FONT_SIZE - 1,
        'ytick.labelsize': FONT_SIZE - 1,
        'legend.fontsize': FONT_SIZE - 1,
        'figure.titlesize': FONT_SIZE + 2,
        'axes.grid': True,
        'grid.alpha': 0.3,
        'axes.spines.top': False,
        'axes.spines.right': False,
    })


def load_metrics(metrics_dir: Path, phase: str) -> Optional[Dict]:
    """Load metrics JSON for a phase."""
    metrics_path = metrics_dir / f'{phase}_metrics.json'
    if not metrics_path.exists():
        print(f"Warning: {metrics_path} not found")
        return None

    with open(metrics_path) as f:
        return json.load(f)


def plot_phase1(metrics: Dict, output_path: Path):
    """Plot Phase 1 (Teacher) training curves."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=FIGSIZE_DOUBLE)

    epochs = metrics['epochs']

    # Loss curves
    ax1.plot(epochs, metrics['train_loss'], label='Train', color=COLORS['train'], linewidth=1.5)
    ax1.plot(epochs, metrics['val_loss'], label='Validation', color=COLORS['val'], linewidth=1.5)

    # Mark best epoch
    if metrics['best_epoch']:
        best_idx = epochs.index(metrics['best_epoch'])
        ax1.axvline(x=metrics['best_epoch'], color='gray', linestyle='--', alpha=0.5, label=f"Best (epoch {metrics['best_epoch']})")
        ax1.scatter([metrics['best_epoch']], [metrics['val_loss'][best_idx]], color=COLORS['val'], s=50, zorder=5)

    ax1.set_xlabel('Epoch')
    ax1.set_ylabel('Triplet Loss')
    ax1.set_title('Phase 1: Teacher Training')
    ax1.legend(loc='upper right')
    ax1.set_xlim(left=1)

    # Learning rate
    ax2.plot(epochs, metrics['learning_rate'], color=COLORS['lr'], linewidth=1.5)
    ax2.set_xlabel('Epoch')
    ax2.set_ylabel('Learning Rate')
    ax2.set_title('Learning Rate Schedule')
    ax2.set_xlim(left=1)
    ax2.yaxis.set_major_formatter(ticker.ScalarFormatter(useMathText=True))
    ax2.ticklabel_format(style='sci', axis='y', scilimits=(0, 0))

    plt.tight_layout()
    plt.savefig(output_path, dpi=DPI, bbox_inches='tight')
    plt.close()
    print(f"Saved: {output_path}")


def plot_phase2(metrics: Dict, output_path: Path):
    """Plot Phase 2 (Student-Teacher alignment) training curves."""
    fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=FIGSIZE_TRIPLE)

    epochs = metrics['epochs']

    # Loss curves
    ax1.plot(epochs, metrics['train_loss'], label='Train', color=COLORS['train'], linewidth=1.5)
    ax1.plot(epochs, metrics['val_loss'], label='Validation', color=COLORS['val'], linewidth=1.5)

    if metrics['best_epoch']:
        ax1.axvline(x=metrics['best_epoch'], color='gray', linestyle='--', alpha=0.5)

    ax1.set_xlabel('Epoch')
    ax1.set_ylabel('Distillation Loss')
    ax1.set_title('Phase 2: Student-Teacher Alignment')
    ax1.legend(loc='upper right')
    ax1.set_xlim(left=1)

    # Cosine similarity
    if 'cosine_sim' in metrics:
        ax2.plot(epochs, metrics['cosine_sim'], color=COLORS['cosine_sim'], linewidth=1.5)
        ax2.set_xlabel('Epoch')
        ax2.set_ylabel('Cosine Similarity')
        ax2.set_title('Student-Teacher Alignment')
        ax2.set_xlim(left=1)
        ax2.set_ylim(0, 1)

        # Add horizontal line at target
        ax2.axhline(y=0.95, color='gray', linestyle='--', alpha=0.5, label='Target (0.95)')
        ax2.legend(loc='lower right')

    # Learning rate
    ax3.plot(epochs, metrics['learning_rate'], color=COLORS['lr'], linewidth=1.5)
    ax3.set_xlabel('Epoch')
    ax3.set_ylabel('Learning Rate')
    ax3.set_title('Learning Rate Schedule')
    ax3.set_xlim(left=1)
    ax3.yaxis.set_major_formatter(ticker.ScalarFormatter(useMathText=True))
    ax3.ticklabel_format(style='sci', axis='y', scilimits=(0, 0))

    plt.tight_layout()
    plt.savefig(output_path, dpi=DPI, bbox_inches='tight')
    plt.close()
    print(f"Saved: {output_path}")


def plot_phase3(metrics: Dict, output_path: Path):
    """Plot Phase 3 (Fine-tuning) training curves."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=FIGSIZE_DOUBLE)

    epochs = metrics['epochs']

    # Loss curves
    ax1.plot(epochs, metrics['train_loss'], label='Train', color=COLORS['train'], linewidth=1.5)
    ax1.plot(epochs, metrics['val_loss'], label='Validation', color=COLORS['val'], linewidth=1.5)

    if metrics['best_epoch']:
        ax1.axvline(x=metrics['best_epoch'], color='gray', linestyle='--', alpha=0.5, label=f"Best (epoch {metrics['best_epoch']})")
        best_idx = epochs.index(metrics['best_epoch'])
        ax1.scatter([metrics['best_epoch']], [metrics['val_loss'][best_idx]], color=COLORS['val'], s=50, zorder=5)

    ax1.set_xlabel('Epoch')
    ax1.set_ylabel('Triplet Loss')
    ax1.set_title('Phase 3: Hard Negative Fine-tuning')
    ax1.legend(loc='upper right')
    ax1.set_xlim(left=1)

    # Learning rate
    ax2.plot(epochs, metrics['learning_rate'], color=COLORS['lr'], linewidth=1.5)
    ax2.set_xlabel('Epoch')
    ax2.set_ylabel('Learning Rate')
    ax2.set_title('Learning Rate Schedule')
    ax2.set_xlim(left=1)
    ax2.yaxis.set_major_formatter(ticker.ScalarFormatter(useMathText=True))
    ax2.ticklabel_format(style='sci', axis='y', scilimits=(0, 0))

    plt.tight_layout()
    plt.savefig(output_path, dpi=DPI, bbox_inches='tight')
    plt.close()
    print(f"Saved: {output_path}")


def plot_combined(metrics_dir: Path, output_path: Path):
    """Plot combined training curves for all phases."""
    phase1 = load_metrics(metrics_dir, 'phase1')
    phase2 = load_metrics(metrics_dir, 'phase2')
    phase3 = load_metrics(metrics_dir, 'phase3')

    if not any([phase1, phase2, phase3]):
        print("No metrics found!")
        return

    # Count available phases
    available = sum(1 for p in [phase1, phase2, phase3] if p)

    fig, axes = plt.subplots(1, available, figsize=(5 * available, 4))
    if available == 1:
        axes = [axes]

    idx = 0

    # Phase 1
    if phase1:
        ax = axes[idx]
        epochs = phase1['epochs']
        ax.plot(epochs, phase1['train_loss'], label='Train', color=COLORS['train'], linewidth=1.5)
        ax.plot(epochs, phase1['val_loss'], label='Val', color=COLORS['val'], linewidth=1.5)
        ax.set_xlabel('Epoch')
        ax.set_ylabel('Loss')
        ax.set_title('Phase 1: Teacher')
        ax.legend(loc='upper right')
        idx += 1

    # Phase 2
    if phase2:
        ax = axes[idx]
        epochs = phase2['epochs']
        ax.plot(epochs, phase2['train_loss'], label='Train', color=COLORS['train'], linewidth=1.5)
        ax.plot(epochs, phase2['val_loss'], label='Val', color=COLORS['val'], linewidth=1.5)
        if 'cosine_sim' in phase2:
            ax2 = ax.twinx()
            ax2.plot(epochs, phase2['cosine_sim'], label='Cos Sim', color=COLORS['cosine_sim'], linewidth=1.5, linestyle='--')
            ax2.set_ylabel('Cosine Similarity', color=COLORS['cosine_sim'])
            ax2.set_ylim(0, 1)
        ax.set_xlabel('Epoch')
        ax.set_ylabel('Loss')
        ax.set_title('Phase 2: Alignment')
        ax.legend(loc='upper right')
        idx += 1

    # Phase 3
    if phase3:
        ax = axes[idx]
        epochs = phase3['epochs']
        ax.plot(epochs, phase3['train_loss'], label='Train', color=COLORS['train'], linewidth=1.5)
        ax.plot(epochs, phase3['val_loss'], label='Val', color=COLORS['val'], linewidth=1.5)
        ax.set_xlabel('Epoch')
        ax.set_ylabel('Loss')
        ax.set_title('Phase 3: Fine-tuning')
        ax.legend(loc='upper right')

    plt.tight_layout()
    plt.savefig(output_path, dpi=DPI, bbox_inches='tight')
    plt.close()
    print(f"Saved: {output_path}")


def main():
    if not MATPLOTLIB_AVAILABLE:
        print("Error: matplotlib required")
        return

    parser = argparse.ArgumentParser(description='Generate training curves for paper')
    parser.add_argument('--metrics-dir', type=Path, default='metrics',
                        help='Directory containing *_metrics.json files')
    parser.add_argument('--output-dir', type=Path, default='/home/stephen/PycharmProjects/indexing/article/figures',
                        help='Directory to save figures')
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    setup_style()

    # Plot individual phases
    phase1 = load_metrics(args.metrics_dir, 'phase1')
    if phase1:
        plot_phase1(phase1, args.output_dir / 'phase1_curves.pdf')

    phase2 = load_metrics(args.metrics_dir, 'phase2')
    if phase2:
        plot_phase2(phase2, args.output_dir / 'phase2_curves.pdf')

    phase3 = load_metrics(args.metrics_dir, 'phase3')
    if phase3:
        plot_phase3(phase3, args.output_dir / 'phase3_curves.pdf')

    # Plot combined
    plot_combined(args.metrics_dir, args.output_dir / 'training_curves.pdf')

    print("Done!")


if __name__ == '__main__':
    main()

