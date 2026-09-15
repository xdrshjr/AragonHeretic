#!/usr/bin/env bash
set -Eeuo pipefail
record=/root/autodl-fs/heretic-runs/ara-v3-pro6000-latest-pilot-20260914T055809Z
work=/root/autodl-tmp/ara-v3-pro6000-latest-pilot-20260914T055809Z
config=/root/autodl-fs/AragonHeretic-v3/config.qwen38-27b-ara-v3-pro6000-pilot-20260914T055809Z.toml
export HERETIC_RESEARCH_PYTHON=/root/autodl-fs/conda-envs/heretic-dsv4/bin/python
export PYTHONPATH=/root/autodl-fs/AragonHeretic-v3/src
export HF_HOME=/root/autodl-fs/hf-cache
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export TOKENIZERS_PARALLELISM=false
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
finish() { code=$?; printf '%s\n' "$code" > "$record/preflight.exit-code"; }
trap finish EXIT
printf 'unit-tests\n' > "$record/current-stage.txt"
cd /root/autodl-fs/AragonHeretic-v3/tests
timeout 300 "$HERETIC_RESEARCH_PYTHON" -m unittest test_ara_refinement test_config -v
printf 'protocol\n' > "$record/current-stage.txt"
cd /root/autodl-fs/AragonHeretic-v3
timeout 600 "$HERETIC_RESEARCH_PYTHON" scripts/prepare_ara_research_protocol.py --config "$record/preparation.json"
printf 'preflight\n' > "$record/current-stage.txt"
timeout --signal=INT --kill-after=120s 1800s bash scripts/run_ara_research_96.sh --config "$config" --run-dir "$work/preflight" --phase preflight
printf 'preflight-complete\n' > "$record/current-stage.txt"
