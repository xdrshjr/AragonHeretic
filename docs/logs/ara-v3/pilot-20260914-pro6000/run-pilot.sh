#!/usr/bin/env bash
set -Eeuo pipefail
export HERETIC_RESEARCH_PYTHON=/root/autodl-fs/conda-envs/heretic-dsv4/bin/python
export PYTHONPATH=/root/autodl-fs/AragonHeretic-v3/src
export HF_HOME=/root/autodl-fs/hf-cache
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export TOKENIZERS_PARALLELISM=false
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
cd /root/autodl-fs/AragonHeretic-v3
set +e
timeout --signal=INT --kill-after=120s 5700s bash scripts/run_ara_research_96.sh \
  --config /root/autodl-fs/AragonHeretic-v3/config.qwen38-27b-ara-v3-pro6000-pilot-20260914T055809Z.toml \
  --run-dir /root/autodl-tmp/ara-v3-pro6000-latest-pilot-20260914T055809Z/pilot \
  --phase pilot
code=$?
set -e
printf '%s\n' "$code" > /root/autodl-fs/heretic-runs/ara-v3-pro6000-latest-pilot-20260914T055809Z/pilot.exit-code
exit "$code"
