#!/usr/bin/env bash
set -Eeuo pipefail

# Full Qwen3.8-27B calibrated ARA run for the single 96 GiB GPU server.
#
# Start a new run:
#   bash scripts/run_qwen38_27b_cara_full.sh
#
# Resume an interrupted run (use the same directory printed at startup):
#   bash scripts/run_qwen38_27b_cara_full.sh /root/autodl-fs/heretic-runs/qwen3.8-27b-cara-full-YYYYMMDDTHHMMSSZ
#
# For a long SSH session, run this script inside tmux. The script appends to
# run.log on resume and never overwrites a completed run or exported adapter.

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_PREFIX="${HERETIC_CONDA_ENV:-/root/autodl-fs/conda-envs/heretic-dsv4}"
RUN_ROOT="${HERETIC_RUN_ROOT:-/root/autodl-fs/heretic-runs}"
CONFIG_SOURCE="${HERETIC_CONFIG:-${PROJECT_ROOT}/config.qwen38-27b-cara-full-single-gpu.toml}"
MODEL_DIR="/root/autodl-fs/models/Qwen3.8-27B"
HF_CACHE="${HERETIC_HF_HOME:-/root/autodl-fs/hf-cache}"
RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${1:-${RUN_ROOT}/qwen3.8-27b-cara-full-${RUN_STAMP}}"
HERETIC_BIN="${ENV_PREFIX}/bin/heretic"
PYTHON_BIN="${ENV_PREFIX}/bin/python"
RUN_MARKER="${RUN_DIR}/.qwen38-27b-cara-full-run"

if [[ ! -x "${HERETIC_BIN}" || ! -x "${PYTHON_BIN}" ]]; then
    echo "Heretic Conda environment is incomplete: ${ENV_PREFIX}" >&2
    exit 1
fi
if [[ ! -f "${CONFIG_SOURCE}" ]]; then
    echo "Full-run configuration not found: ${CONFIG_SOURCE}" >&2
    exit 1
fi
if [[ ! -f "${MODEL_DIR}/model.safetensors.index.json" ]]; then
    echo "Qwen3.8-27B local model is incomplete or missing: ${MODEL_DIR}" >&2
    exit 1
fi
if [[ ! -d "${HF_CACHE}" ]]; then
    echo "Hugging Face cache not found: ${HF_CACHE}" >&2
    exit 1
fi

RESUMING=0
if [[ -e "${RUN_DIR}" ]]; then
    if [[ ! -f "${RUN_MARKER}" || ! -f "${RUN_DIR}/config.toml" ]]; then
        echo "Refusing to use an existing directory not created by this script: ${RUN_DIR}" >&2
        exit 1
    fi
    if [[ -f "${RUN_DIR}/exit-code.txt" ]] && [[ "$(<"${RUN_DIR}/exit-code.txt")" == "0" ]]; then
        echo "This run is already complete: ${RUN_DIR}" >&2
        exit 1
    fi
    RESUMING=1
else
    mkdir -p "${RUN_DIR}"
    cp "${CONFIG_SOURCE}" "${RUN_DIR}/config.toml"
    : > "${RUN_MARKER}"
fi

exec 9>"${RUN_DIR}/run.lock"
if ! flock -n 9; then
    echo "Another process is already using this run directory: ${RUN_DIR}" >&2
    exit 1
fi

export CUDA_VISIBLE_DEVICES="${HERETIC_CUDA_VISIBLE_DEVICES:-0}"
export HF_HOME="${HF_CACHE}"
export PYTHONUNBUFFERED=1
export TOKENIZERS_PARALLELISM=false

# The model and all pinned dataset revisions are already cached on this server.
# Offline mode avoids the machine-wide incomplete mirror and optional local proxy.
if [[ "${HERETIC_OFFLINE:-1}" == "1" ]]; then
    export HF_HUB_OFFLINE=1
    export HF_DATASETS_OFFLINE=1
    unset HF_ENDPOINT HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
else
    unset HF_HUB_OFFLINE HF_DATASETS_OFFLINE
    export HF_ENDPOINT="${HERETIC_HF_ENDPOINT:-https://huggingface.co}"
    if [[ -n "${HERETIC_HTTP_PROXY:-}" ]]; then
        export HTTP_PROXY="${HERETIC_HTTP_PROXY}"
        export HTTPS_PROXY="${HERETIC_HTTPS_PROXY:-${HERETIC_HTTP_PROXY}}"
        export http_proxy="${HTTP_PROXY}"
        export https_proxy="${HTTPS_PROXY}"
    else
        unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
    fi
fi

GPU_FREE_MIB="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits -i 0 | head -n 1 | tr -d '[:space:]')"
MIN_GPU_FREE_MIB="${HERETIC_MIN_GPU_FREE_MIB:-70000}"
if [[ ! "${GPU_FREE_MIB}" =~ ^[0-9]+$ ]]; then
    echo "Unable to determine free memory for GPU 0." >&2
    exit 1
fi
if (( GPU_FREE_MIB < MIN_GPU_FREE_MIB )); then
    echo "GPU 0 has only ${GPU_FREE_MIB} MiB free; at least ${MIN_GPU_FREE_MIB} MiB is required." >&2
    echo "Stop other GPU jobs, or deliberately override HERETIC_MIN_GPU_FREE_MIB." >&2
    exit 1
fi

cd "${RUN_DIR}"

# Parse the exact run-local config and verify every full calibration,
# validation, and audit split before allocating the 27B model.
"${PYTHON_BIN}" - <<'PY'
from datasets import load_dataset
from heretic.config import AbliterationMethod, Settings

settings = Settings()
assert settings.abliteration_method == AbliterationMethod.ARA
assert settings.ara_lora_rank == 128
assert settings.ara_calibration_size == 64
assert settings.n_trials == 120
assert settings.acceptance_gate is not None

specifications = [settings.good_prompts, settings.bad_prompts]
for scorer in (settings.model_extra or {}).get("scorer", {}).values():
    prompts = scorer.get("prompts")
    if prompts:
        specifications.append(type(settings.good_prompts).model_validate(prompts))
specifications.extend(
    [
        settings.acceptance_gate.keyword_audit_prompts,
        settings.acceptance_gate.kl_audit_prompts,
    ]
)
for spec in specifications:
    dataset = load_dataset(
        spec.dataset,
        revision=spec.commit,
        split=spec.split,
    )
    if not dataset or spec.column not in dataset.column_names:
        raise RuntimeError(f"Dataset preflight failed: {spec.dataset} {spec.split}")

print("Full ARA configuration and all six dataset slices passed preflight.")
PY

git -C "${PROJECT_ROOT}" rev-parse HEAD > "${RUN_DIR}/source-commit.txt"
nvidia-smi --query-gpu=index,name,memory.total,memory.free,driver_version \
    --format=csv,noheader > "${RUN_DIR}/gpu-before.csv"
"${PYTHON_BIN}" -V > "${RUN_DIR}/python-version.txt" 2>&1

if (( RESUMING )); then
    echo "Resuming full Qwen3.8-27B ARA run: ${RUN_DIR}" | tee -a "${RUN_DIR}/run.log"
else
    echo "Starting full Qwen3.8-27B ARA run: ${RUN_DIR}" | tee "${RUN_DIR}/run.log"
fi
echo "Configuration: ${RUN_DIR}/config.toml" | tee -a "${RUN_DIR}/run.log"
echo "Checkpoints: ${RUN_DIR}/checkpoints" | tee -a "${RUN_DIR}/run.log"
echo "Final adapter: ${RUN_DIR}/adapter" | tee -a "${RUN_DIR}/run.log"

set +e
"${HERETIC_BIN}" 2>&1 | tee -a "${RUN_DIR}/run.log"
HERETIC_EXIT=${PIPESTATUS[0]}
set -e

nvidia-smi --query-gpu=index,memory.used,memory.free,utilization.gpu \
    --format=csv,noheader > "${RUN_DIR}/gpu-after.csv" || true
printf '%s\n' "${HERETIC_EXIT}" > "${RUN_DIR}/exit-code.txt"

if [[ ${HERETIC_EXIT} -ne 0 ]]; then
    echo "Full ARA run stopped with exit code ${HERETIC_EXIT}." >&2
    echo "Resume with: bash ${PROJECT_ROOT}/scripts/run_qwen38_27b_cara_full.sh ${RUN_DIR}" >&2
    exit "${HERETIC_EXIT}"
fi

set +e
"${PYTHON_BIN}" - "${RUN_DIR}/adapter" "${RUN_DIR}/acceptance.json" <<'PY'
import json
import sys
from pathlib import Path

adapter = Path(sys.argv[1])
report_path = Path(sys.argv[2])
if not adapter.is_dir():
    raise SystemExit("accepted adapter directory is missing")
report = json.loads(report_path.read_text(encoding="utf-8"))
if report.get("status") != "passed":
    raise SystemExit("acceptance status is not passed")
if report.get("selected_trial_number") is None:
    raise SystemExit("accepted trial number is missing")
if not (adapter / "adapter_config.json").is_file():
    raise SystemExit("adapter_config.json is missing")
weights = list(adapter.glob("*.safetensors"))
if not weights or any(path.stat().st_size == 0 for path in weights):
    raise SystemExit("non-empty adapter safetensors are missing")
PY
POSTCHECK_EXIT=$?
set -e
if [[ ${POSTCHECK_EXIT} -ne 0 ]]; then
    printf '%s\n' 90 > "${RUN_DIR}/exit-code.txt"
    echo "Full ARA artifact post-check failed." >&2
    exit 90
fi

echo "Full ARA run completed successfully."
echo "Adapter: ${RUN_DIR}/adapter"
echo "Acceptance report: ${RUN_DIR}/acceptance.json"
