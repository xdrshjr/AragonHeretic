#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Latest ARA one-command launcher.

set -Eeuo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# User-editable settings. Command-line options override these values.
# Data counts are per side: one harmless side and one harmful side.
# ---------------------------------------------------------------------------
ARA_MODE="${ARA_MODE:-direct}"                 # direct | research
MODEL="${ARA_MODEL:-/root/autodl-fs/models/Qwen3.8-27B}"
CONDA_ENV="${ARA_CONDA_ENV:-/root/autodl-fs/conda-envs/heretic-dsv4}"
HF_CACHE="${ARA_HF_CACHE:-/root/autodl-fs/hf-cache}"
RUN_ROOT="${ARA_RUN_ROOT:-/root/autodl-fs/heretic-runs}"
RUN_DIR="${ARA_RUN_DIR:-}"
SEED="${ARA_SEED:-42}"

# Direct engineering adaptation defaults. Edit these values as needed.
FIT_SAMPLES="${ARA_FIT_SAMPLES:-8}"
MONITOR_SAMPLES="${ARA_MONITOR_SAMPLES:-8}"
DEVELOPMENT_SAMPLES="${ARA_DEVELOPMENT_SAMPLES:-20}"
TRIALS="${ARA_TRIALS:-1}"
MAX_HOURS="${ARA_MAX_HOURS:-8}"

# Formal research mode only. These cannot be fabricated or left as examples.
TARGET_CONTRACT="${ARA_TARGET_EXECUTION_CONTRACT:-}"
READINESS_EVIDENCE="${ARA_READINESS_EVIDENCE:-}"
PILOT_RECORD="${ARA_PILOT_RECORD:-/root/autodl-fs/heretic-runs/ara-v3-pro6000-smoke-20260911}"
RESEARCH_HOURS="${ARA_RESEARCH_HOURS:-48}"

usage() {
    cat <<'EOF'
用法：
  ./run_latest_ara.sh [选项]

默认运行 direct 工程适配模式：直接使用最新的
S2/sequential-refresh + spectral-backtrack-v1，完成后导出 PEFT adapter。
该模式不要求 target contract/search_readiness，也不代表正式研究验收。

常用选项：
  --mode direct|research       运行模式，默认 direct
  --model PATH                 模型目录
  --run-root PATH              自动创建运行目录的父目录
  --run-dir PATH               指定本次运行目录
  --seed 42|43|44              随机种子
  --conda-env PATH             Conda 环境目录
  --hf-cache PATH              Hugging Face 缓存目录

direct 模式参数（数量均为每侧）：
  --fit-samples N              拟合数据数量，默认 8
  --monitor-samples N          每个块的门控数据数量，默认 8
  --development-samples N      trial 选择数据数量，默认 20
  --trials N                   尝试参数组数量，默认 1，最大 24
  --max-hours N                墙钟时间上限，默认 8 小时

research 正式模式参数：
  --target-execution-contract PATH
  --readiness-evidence PATH
  --pilot-record PATH
  --hours N                    正式实验预算，默认 48 小时
  --prepare-only
  --resume RUN_DIR
  --status RUN_DIR

其他：
  --dry-run                    只检查并显示参数，不加载模型
  -h, --help                   显示帮助

示例：
  ./run_latest_ara.sh

  ./run_latest_ara.sh --fit-samples 16 --monitor-samples 12 \
    --development-samples 40 --trials 4 --max-hours 24

  ./run_latest_ara.sh --mode research \
    --target-execution-contract /real/path/target.json \
    --readiness-evidence /real/path/search-readiness.json
EOF
}

die() {
    printf '错误：%s\n' "$*" >&2
    exit 2
}

need_value() {
    [[ $# -ge 2 && -n "${2:-}" ]] || die "$1 缺少参数值"
}

print_command() {
    printf '实际命令：\n  '
    printf '%q ' "$@"
    printf '\n'
}

PREPARE_ONLY=0
DRY_RUN=0
RESUME_RUN=""
STATUS_RUN=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)
            need_value "$@"; ARA_MODE="$2"; shift 2 ;;
        --model)
            need_value "$@"; MODEL="$2"; shift 2 ;;
        --conda-env)
            need_value "$@"; CONDA_ENV="$2"; shift 2 ;;
        --hf-cache)
            need_value "$@"; HF_CACHE="$2"; shift 2 ;;
        --run-root)
            need_value "$@"; RUN_ROOT="$2"; shift 2 ;;
        --run-dir)
            need_value "$@"; RUN_DIR="$2"; shift 2 ;;
        --seed)
            need_value "$@"; SEED="$2"; shift 2 ;;
        --fit-samples)
            need_value "$@"; FIT_SAMPLES="$2"; shift 2 ;;
        --monitor-samples)
            need_value "$@"; MONITOR_SAMPLES="$2"; shift 2 ;;
        --development-samples)
            need_value "$@"; DEVELOPMENT_SAMPLES="$2"; shift 2 ;;
        --trials)
            need_value "$@"; TRIALS="$2"; shift 2 ;;
        --max-hours)
            need_value "$@"; MAX_HOURS="$2"; shift 2 ;;
        --target-execution-contract)
            need_value "$@"; TARGET_CONTRACT="$2"; shift 2 ;;
        --readiness-evidence)
            need_value "$@"; READINESS_EVIDENCE="$2"; shift 2 ;;
        --pilot-record)
            need_value "$@"; PILOT_RECORD="$2"; shift 2 ;;
        --hours)
            need_value "$@"; RESEARCH_HOURS="$2"; shift 2 ;;
        --prepare-only)
            PREPARE_ONLY=1; shift ;;
        --resume)
            need_value "$@"; RESUME_RUN="$2"; shift 2 ;;
        --status)
            need_value "$@"; STATUS_RUN="$2"; shift 2 ;;
        --dry-run)
            DRY_RUN=1; shift ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            die "未知参数：$1（使用 --help 查看帮助）" ;;
    esac
done

[[ "$ARA_MODE" == "direct" || "$ARA_MODE" == "research" ]] ||
    die "--mode 必须是 direct 或 research"

PYTHON="$CONDA_ENV/bin/python"
[[ -x "$PYTHON" ]] || die "Python 环境不存在：$PYTHON"

export HERETIC_RESEARCH_PYTHON="$PYTHON"
export PYTHONPATH="$PROJECT_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1
export TOKENIZERS_PARALLELISM=false
export HF_HOME="$HF_CACHE"
export HUGGINGFACE_HUB_CACHE="$HF_CACHE/hub"
export TRANSFORMERS_CACHE="$HF_CACHE/hub"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export HF_DATASETS_OFFLINE="${HF_DATASETS_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-8}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-8}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-8}"

if [[ "$ARA_MODE" == "direct" ]]; then
    [[ -z "$TARGET_CONTRACT$READINESS_EVIDENCE$RESUME_RUN$STATUS_RUN" ]] ||
        die "contract/readiness/resume/status 仅用于 --mode research"
    [[ "$PREPARE_ONLY" -eq 0 ]] ||
        die "--prepare-only 仅用于 --mode research"

    command=(
        "$PYTHON" -m heretic.engineering_direct
        --model "$MODEL"
        --run-root "$RUN_ROOT"
        --seed "$SEED"
        --fit-samples "$FIT_SAMPLES"
        --monitor-samples "$MONITOR_SAMPLES"
        --development-samples "$DEVELOPMENT_SAMPLES"
        --trials "$TRIALS"
        --max-hours "$MAX_HOURS"
    )
    [[ -z "$RUN_DIR" ]] || command+=(--run-dir "$RUN_DIR")
    [[ "$DRY_RUN" -eq 0 ]] || command+=(--dry-run)

    printf '%s\n' '模式：direct 工程适配（非正式研究验收）'
    printf '每侧数据：fit=%s，monitor=%s，development=%s；trials=%s\n' \
        "$FIT_SAMPLES" "$MONITOR_SAMPLES" "$DEVELOPMENT_SAMPLES" "$TRIALS"
    print_command "${command[@]}"
    exec "${command[@]}"
fi

[[ -z "$RUN_DIR" ]] || die "research 模式请使用 --run-root，而不是 --run-dir"
[[ -z "$RESUME_RUN" || -z "$STATUS_RUN" ]] ||
    die "--resume 和 --status 不能同时使用"
[[ -z "$RESUME_RUN$STATUS_RUN" || "$PREPARE_ONLY" -eq 0 ]] ||
    die "--prepare-only 不能和 --resume/--status 同时使用"

launcher="$PROJECT_ROOT/scripts/run_qwen38_27b_ara_v3_pro6000.sh"
[[ -f "$launcher" ]] || die "正式研究启动器不存在：$launcher"
command=(bash "$launcher")

if [[ -n "$RESUME_RUN" ]]; then
    command+=(--resume "$RESUME_RUN")
elif [[ -n "$STATUS_RUN" ]]; then
    command+=(--status "$STATUS_RUN")
else
    [[ -n "$TARGET_CONTRACT" ]] ||
        die "research 模式需要真实的 --target-execution-contract"
    [[ -n "$READINESS_EVIDENCE" ]] ||
        die "research 模式需要真实的 --readiness-evidence"
    command+=(
        --target-execution-contract "$TARGET_CONTRACT"
        --readiness-evidence "$READINESS_EVIDENCE"
        --pilot-record "$PILOT_RECORD"
        --model "$MODEL"
        --run-root "$RUN_ROOT"
        --seed "$SEED"
        --hours "$RESEARCH_HOURS"
    )
    [[ "$PREPARE_ONLY" -eq 0 ]] || command+=(--prepare-only)
fi

printf '%s\n' '模式：research 正式研究（要求完整证据链）'
print_command "${command[@]}"
[[ "$DRY_RUN" -eq 0 ]] || exit 0

if [[ -z "$RESUME_RUN$STATUS_RUN" ]]; then
    [[ -d "$MODEL" ]] || die "模型目录不存在：$MODEL"
    [[ -f "$TARGET_CONTRACT" ]] || die "目标执行契约不存在：$TARGET_CONTRACT"
    [[ -f "$READINESS_EVIDENCE" ]] || die "readiness 不存在：$READINESS_EVIDENCE"
    [[ -d "$PILOT_RECORD" ]] || die "pilot 目录不存在：$PILOT_RECORD"
fi

exec "${command[@]}"
