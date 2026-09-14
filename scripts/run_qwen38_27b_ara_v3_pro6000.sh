#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# 新实验需 --target-execution-contract 和 --readiness-evidence。
# 旧 --resume 沿用其冻结源码；阶段退出码由 Python 入口透传。
set -euo pipefail
project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
experiment_python=${HERETIC_RESEARCH_PYTHON:-/root/autodl-fs/conda-envs/heretic-dsv4/bin/python}
if [[ ! -x "$experiment_python" ]]; then
    printf 'Python 环境不存在：%s\n' "$experiment_python" >&2
    exit 2
fi
export PYTHONPATH="$project/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1 TOKENIZERS_PARALLELISM=false
export HF_HOME=${HF_HOME:-/root/autodl-fs/hf-cache}
export HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1
export OMP_NUM_THREADS=8 MKL_NUM_THREADS=8 OPENBLAS_NUM_THREADS=8
exec "$experiment_python" -m heretic.pro6000_launch "$@"
