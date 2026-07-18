#!/usr/bin/env bash
# One-time: create the GB-STAMP CPU env on /vast (shared to CRC compute nodes + pitt).
# The vLLM GPU env is the GOTW-shared /vast/ishi/envs/vllm (HF cache /vast/ishi/hf_cache).
set -euo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh 2>/dev/null \
  || source /home/gazetteer/miniconda/etc/profile.d/conda.sh
conda create -p /vast/ishi/envs/boundary python=3.11 -y
PY=/vast/ishi/envs/boundary/bin/pip
# torch: the a100 driver is CUDA 12.9 -> use cu124 wheels (cu13 wheels fail "driver too old")
$PY install torch --index-url https://download.pytorch.org/whl/cu124
# geo + CV + ML (CPU): crop stitching, admin-join KDTree, spotter-adjacent tooling
$PY install opencv-python-headless scikit-image scikit-learn pillow "numpy<2.3" pyshp shapely scipy
/vast/ishi/envs/boundary/bin/python -c "import torch,cv2,skimage,sklearn,shapely,shapefile,scipy,PIL; print('boundary env OK', torch.__version__)"
