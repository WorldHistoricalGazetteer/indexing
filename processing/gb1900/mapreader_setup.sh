#!/usr/bin/env bash
# MapReader + MapTextPipeline text-spotting setup (the "notoriously tricky" one).
# Runs on the pitt VM (CPU). RECONSTRUCTED from the setup done 2026-07-18; the AUTHORITATIVE
# pinned versions live in ./mapreader_env.lock (pip freeze) + ./mapreader_conda.lock — regenerate
# with:  conda run -n mapreader pip freeze > mapreader_env.lock; conda list -n mapreader > mapreader_conda.lock
set -euo pipefail
source /home/gazetteer/miniconda/etc/profile.d/conda.sh
conda create -n mapreader python=3.11 -y
conda activate mapreader
# C/C++ toolchain — detectron2 + MapTextPipeline build native ops; the VM has no system compiler
conda install --override-channels -c conda-forge gxx_linux-64=12 gcc_linux-64=12 ninja -y
pip install "numpy<2" opencv-python-headless
pip install mapreader                                   # 1.8.2 at time of writing (CPU torch 2.2.2+cu121)
pip install 'git+https://github.com/facebookresearch/detectron2.git'   # 0.6, FAIR main
# MapTextPipeline (Living-with-Machines / rwood-97 CPU fork) + Rumsey-finetuned weights:
#   git clone the fork, pip install -e it, and place rumsey-finetune.pth (ViTAEv2-S) under weights/.
#   config: MapTextPipeline/configs/ViTAEv2_S/rumsey/final_rumsey.yaml
echo "NOTE: clone MapTextPipeline + fetch rumsey-finetune.pth per README; then verify against mapreader_env.lock"
