#!/usr/bin/env bash
set -Eeuo pipefail

# One-command Qwen3.8-27B calibrated ARA smoke run for the adaptation server.
# Usage:
#   bash scripts/run_qwen38_27b_cara_smoke.sh
#   bash scripts/run_qwen38_27b_cara_smoke.sh /root/autodl-fs/heretic-runs/my-run

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_PREFIX="${HERETIC_CONDA_ENV:-/root/autodl-fs/conda-envs/heretic-dsv4}"
RUN_ROOT="${HERETIC_RUN_ROOT:-/root/autodl-fs/heretic-runs}"
CONFIG_SOURCE="${HERETIC_CONFIG:-${PROJECT_ROOT}/config.qwen38-27b-cara-smoke.toml}"
RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${1:-${RUN_ROOT}/qwen3.8-27b-cara-smoke-${RUN_STAMP}}"
HERETIC_BIN="${ENV_PREFIX}/bin/heretic"

if [[ ! -x "${HERETIC_BIN}" ]]; then
    echo "Heretic executable not found: ${HERETIC_BIN}" >&2
    exit 1
fi
if [[ ! -f "${CONFIG_SOURCE}" ]]; then
    echo "Configuration not found: ${CONFIG_SOURCE}" >&2
    exit 1
fi
if [[ ! -f /root/autodl-fs/models/Qwen3.8-27B/model.safetensors.index.json ]]; then
    echo "Qwen3.8-27B local model is incomplete or missing." >&2
    exit 1
fi
if [[ -e "${RUN_DIR}" ]]; then
    echo "Refusing to reuse existing run directory: ${RUN_DIR}" >&2
    exit 1
fi

mkdir -p "${RUN_DIR}"
cp "${CONFIG_SOURCE}" "${RUN_DIR}/config.toml"
git -C "${PROJECT_ROOT}" rev-parse HEAD > "${RUN_DIR}/source-commit.txt"
nvidia-smi --query-gpu=index,name,memory.total,memory.free,driver_version \
    --format=csv,noheader > "${RUN_DIR}/gpu-before.csv"

# Both pinned datasets are prepared in this cache. Offline mode is the default
# because the server-wide mirror is incomplete and the local proxy is optional.
export HF_HOME="${HERETIC_HF_HOME:-/root/autodl-fs/hf-cache}"
if [[ "${HERETIC_OFFLINE:-1}" == "1" ]]; then
    export HF_HUB_OFFLINE=1
    export HF_DATASETS_OFFLINE=1
    unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
else
    export HF_ENDPOINT="${HERETIC_HF_ENDPOINT:-https://huggingface.co}"
    export HTTP_PROXY="${HERETIC_HTTP_PROXY:-http://127.0.0.1:7890}"
    export HTTPS_PROXY="${HERETIC_HTTPS_PROXY:-${HTTP_PROXY}}"
    export http_proxy="${HTTP_PROXY}"
    export https_proxy="${HTTPS_PROXY}"
fi
export PYTHONUNBUFFERED=1
export TOKENIZERS_PARALLELISM=false

echo "Project: ${PROJECT_ROOT}"
echo "Environment: ${ENV_PREFIX}"
echo "Run directory: ${RUN_DIR}"
echo "Configuration: ${RUN_DIR}/config.toml"

cd "${RUN_DIR}"
set +e
"${HERETIC_BIN}" 2>&1 | tee "${RUN_DIR}/run.log"
HERETIC_EXIT=${PIPESTATUS[0]}
set -e

nvidia-smi --query-gpu=index,memory.used,memory.free,utilization.gpu \
    --format=csv,noheader > "${RUN_DIR}/gpu-after.csv"
printf '%s\n' "${HERETIC_EXIT}" > "${RUN_DIR}/exit-code.txt"

if [[ ${HERETIC_EXIT} -ne 0 ]]; then
    echo "CARA smoke run failed; see ${RUN_DIR}/run.log" >&2
    exit "${HERETIC_EXIT}"
fi

echo "CARA smoke run completed. Adapter: ${RUN_DIR}/adapter"
