#!/usr/bin/env bash
set -Eeuo pipefail

# Pre-registered trajectory-v2 run for the DeepSeek-V4 adaptation server.
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_PREFIX="${HERETIC_CONDA_ENV:-/root/autodl-fs/conda-envs/heretic-dsv4}"
RUN_ROOT="${HERETIC_RUN_ROOT:-/root/autodl-fs/heretic-runs}"
CONFIG_SOURCE="${HERETIC_CONFIG:-${PROJECT_ROOT}/config.qwen38-27b-cara-v2.toml}"
HF_CACHE="${HERETIC_HF_HOME:-/root/autodl-fs/hf-cache}"
RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${1:-${RUN_ROOT}/qwen38-cara-trajectory-v2-${RUN_STAMP}}"
HERETIC_BIN="${ENV_PREFIX}/bin/heretic"
PYTHON_BIN="${ENV_PREFIX}/bin/python"
RUN_MARKER="${RUN_DIR}/.qwen38-cara-trajectory-v2"

if [[ ! -x "${HERETIC_BIN}" || ! -x "${PYTHON_BIN}" ]]; then
    echo "Heretic environment is incomplete: ${ENV_PREFIX}" >&2
    exit 1
fi
if [[ ! -f "${CONFIG_SOURCE}" ]]; then
    echo "trajectory-v2 configuration is missing: ${CONFIG_SOURCE}" >&2
    exit 1
fi
if [[ ! -d "${HF_CACHE}" ]]; then
    echo "Hugging Face cache is missing: ${HF_CACHE}" >&2
    exit 1
fi

if [[ -e "${RUN_DIR}" ]]; then
    if [[ ! -f "${RUN_MARKER}" || ! -f "${RUN_DIR}/config.toml" ]]; then
        echo "Refusing an existing directory not owned by this wrapper." >&2
        exit 1
    fi
    if [[ -f "${RUN_DIR}/exit-code.txt" ]] &&
        [[ "$(<"${RUN_DIR}/exit-code.txt")" == "0" ]]; then
        echo "Run is already complete: ${RUN_DIR}" >&2
        exit 1
    fi
else
    mkdir -p "${RUN_DIR}"
    cp "${CONFIG_SOURCE}" "${RUN_DIR}/config.toml"
    : > "${RUN_MARKER}"
fi

exec 9>"${RUN_DIR}/run.lock"
if ! flock -n 9; then
    echo "Another process owns this trajectory-v2 run." >&2
    exit 1
fi

export CUDA_VISIBLE_DEVICES="${HERETIC_CUDA_VISIBLE_DEVICES:-0}"
export HF_HOME="${HF_CACHE}"
export PYTHONUNBUFFERED=1
export TOKENIZERS_PARALLELISM=false
if [[ "${HERETIC_OFFLINE:-1}" == "1" ]]; then
    export HF_HUB_OFFLINE=1
    export HF_DATASETS_OFFLINE=1
    unset HF_ENDPOINT HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
fi

GPU_FREE_MIB="$(
    nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits -i 0 |
        head -n 1 | tr -d '[:space:]'
)"
MIN_GPU_FREE_MIB="${HERETIC_MIN_GPU_FREE_MIB:-80000}"
if [[ ! "${GPU_FREE_MIB}" =~ ^[0-9]+$ ]] ||
    (( GPU_FREE_MIB < MIN_GPU_FREE_MIB )); then
    echo "GPU 0 does not meet the ${MIN_GPU_FREE_MIB} MiB free-memory gate." >&2
    exit 1
fi

cd "${RUN_DIR}"
"${PYTHON_BIN}" - <<'PY'
from heretic.config import Settings
from heretic.protocol_data import preflight_audit_metadata

settings = Settings()
assert settings.ara_objective_version == "trajectory-v2"
assert settings.n_trials == 120
assert settings.n_startup_trials == 24
assert len(settings.ara_seed_trials) == 8
assert settings.ara_runtime_guard is not None
assert settings.acceptance_gate is not None
preflight_audit_metadata(settings.acceptance_gate.keyword_audit_prompts)
preflight_audit_metadata(settings.acceptance_gate.kl_audit_prompts)
print("Configuration and audit metadata passed preflight without reading rows.")
PY

git -C "${PROJECT_ROOT}" rev-parse HEAD > "${RUN_DIR}/source-commit.txt"
git -C "${PROJECT_ROOT}" status --porcelain > "${RUN_DIR}/source-dirty-state.txt"
nvidia-smi --query-gpu=index,name,memory.total,memory.free,driver_version \
    --format=csv,noheader > "${RUN_DIR}/gpu-before.csv"
"${PYTHON_BIN}" -V > "${RUN_DIR}/python-version.txt" 2>&1

echo "Starting/resuming trajectory-v2 run: ${RUN_DIR}" | tee -a run.log
set +e
"${HERETIC_BIN}" --recover-orphaned-trials --checkpoint-action continue \
    2>&1 | tee -a run.log
HERETIC_EXIT=${PIPESTATUS[0]}
set -e
printf '%s\n' "${HERETIC_EXIT}" > exit-code.txt

if [[ ${HERETIC_EXIT} -ne 0 ]]; then
    echo "trajectory-v2 stopped with exit code ${HERETIC_EXIT}." >&2
    exit "${HERETIC_EXIT}"
fi

set +e
"${PYTHON_BIN}" - "${RUN_DIR}/adapter" "${RUN_DIR}/acceptance.json" <<'PY'
import json
import sys
from pathlib import Path

from heretic.artifact_schema import verify_artifact_graph

adapter = Path(sys.argv[1])
external_report = Path(sys.argv[2])
verify_artifact_graph(adapter)
report_path = adapter / "acceptance.json"
report = json.loads(report_path.read_text(encoding="utf-8"))
if report.get("status") != "passed":
    raise SystemExit("acceptance status is not passed")
if report.get("selected_trial_number") is None:
    raise SystemExit("accepted trial number is missing")
if report.get("audit_consumed") is not True:
    raise SystemExit("audit consumption evidence is missing")
if not (adapter / "adapter_config.json").is_file():
    raise SystemExit("adapter_config.json is missing")
weights = list(adapter.glob("*.safetensors"))
if not weights or any(path.stat().st_size == 0 for path in weights):
    raise SystemExit("non-empty adapter safetensors are missing")
if external_report.read_bytes() != report_path.read_bytes():
    raise SystemExit("external acceptance report differs from adapter report")
PY
POSTCHECK_EXIT=$?
set -e
if [[ ${POSTCHECK_EXIT} -ne 0 ]]; then
    printf '%s\n' 90 > exit-code.txt
    echo "trajectory-v2 artifact post-check failed." >&2
    exit 90
fi

echo "trajectory-v2 completed successfully."
echo "Adapter: ${RUN_DIR}/adapter"
echo "Acceptance report: ${RUN_DIR}/acceptance.json"
