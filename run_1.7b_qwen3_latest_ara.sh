#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Qwen3-1.7B: sequential-v3 + spectral-backtrack-v1, single-GPU engineering run.
set -Eeuo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Edit defaults here, set ARA_* variables, or override with command-line flags.
DATA_ROOT="$HOME"
DEFAULT_ENV="$HOME/miniconda3/envs/aragon-heretic"
DEFAULT_CACHE="$HOME/.cache/huggingface"
if [[ -d /root/autodl-fs && -r /root/autodl-fs ]]; then
    DATA_ROOT=/root/autodl-fs
    DEFAULT_ENV="$DATA_ROOT/conda-envs/heretic-dsv4"
    DEFAULT_CACHE="$DATA_ROOT/hf-cache"
fi
MODEL="${ARA_MODEL:-$DATA_ROOT/models/Qwen3-1.7B}"
CONDA_ENV="${ARA_CONDA_ENV:-${CONDA_PREFIX:-$DEFAULT_ENV}}"
PYTHON="${ARA_PYTHON:-}"
HF_CACHE="${ARA_HF_CACHE:-${HF_HOME:-$DEFAULT_CACHE}}"
RUN_ROOT="${ARA_RUN_ROOT:-$DATA_ROOT/heretic-runs/qwen3-1.7b}"
CONFIG="${ARA_CONFIG_TEMPLATE:-$PROJECT_ROOT/config.qwen3-1.7b-ara-v3.toml}"
GPU="${ARA_GPU:-${CUDA_VISIBLE_DEVICES:-0}}"
extra_args=()

usage() {
    cat <<'EOF'
用法：bash run_1.7b_qwen3_latest_ara.sh [选项]

默认：本地 Qwen3-1.7B，单卡 BF16，rank 128，最多两轮顺序更新及谱回溯。
每侧 fit=8、monitor=8、development=20，1 个 trial，8 小时时限。

  --model PATH                本地模型目录
  --python PATH               Python 可执行文件（需已安装项目依赖）
  --conda-env PATH            Conda 环境目录
  --gpu INDEX|UUID           使用的单张物理 GPU，默认 0
  --hf-cache PATH            Hugging Face 缓存目录
  --run-root PATH            运行目录的父目录
  --run-dir PATH             指定空的运行目录
  --config-template PATH     自定义配置模板
  --fit-samples N            每侧拟合数量
  --monitor-samples N        每侧监控数量
  --development-samples N    每侧开发验证数量（三者总和不能超过 400）
  --trials N                 参数尝试次数，1–24
  --seed 42|43|44            随机种子
  --max-hours N              适配阶段时间上限
  --offline                  仅使用本地缓存的数据
  --dry-run                  打印运行参数，不加载模型、不创建运行目录
  -h, --help                 显示帮助

示例：
  bash run_1.7b_qwen3_latest_ara.sh --dry-run
  bash run_1.7b_qwen3_latest_ara.sh --model /models/Qwen3-1.7B --gpu 1
  bash run_1.7b_qwen3_latest_ara.sh --fit-samples 96 --monitor-samples 64 \
    --development-samples 100 --trials 24 --max-hours 48

默认允许下载缺失的数据集缓存；模型须提前放在本地。
成功结果写入运行目录的 engineering-result.json，其中记录 adapter 路径。
该入口为 direct 工程适配，不要求正式研究的 contract/readiness。
EOF
}

die() { printf '错误：%s\n' "$*" >&2; exit 2; }
need_value() { [[ $# -ge 2 && -n "${2:-}" ]] || die "$1 缺少参数值"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --model) need_value "$@"; MODEL="$2"; shift 2 ;;
        --python) need_value "$@"; PYTHON="$2"; shift 2 ;;
        --conda-env)
            need_value "$@"; CONDA_ENV="$2"; PYTHON="$CONDA_ENV/bin/python"; shift 2 ;;
        --gpu) need_value "$@"; GPU="$2"; shift 2 ;;
        --hf-cache) need_value "$@"; HF_CACHE="$2"; shift 2 ;;
        --run-root) need_value "$@"; RUN_ROOT="$2"; shift 2 ;;
        --config-template) need_value "$@"; CONFIG="$2"; shift 2 ;;
        --offline)
            export HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 TRANSFORMERS_OFFLINE=1
            shift ;;
        --run-dir|--fit-samples|--monitor-samples|--development-samples|--trials|--seed|--max-hours)
            need_value "$@"; extra_args+=("$1" "$2"); shift 2 ;;
        --dry-run) extra_args+=("$1"); shift ;;
        *) die "未知参数：$1（使用 --help 查看帮助）" ;;
    esac
done

[[ -n "$GPU" && "$GPU" != *,* && "$GPU" != "-1" ]] || die "--gpu 必须指定单张 GPU"
[[ -f "$CONFIG" ]] || die "配置模板不存在：$CONFIG"
if [[ -z "$PYTHON" ]]; then
    PYTHON="$CONDA_ENV/bin/python"
fi
[[ -n "$PYTHON" ]] && command -v "$PYTHON" >/dev/null 2>&1 || die "未找到 Python，请使用 --python 或 --conda-env"

export CUDA_VISIBLE_DEVICES="$GPU"
export PYTHONPATH="$PROJECT_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1 TOKENIZERS_PARALLELISM=false
export HF_HOME="$HF_CACHE" HF_HUB_CACHE="$HF_CACHE/hub"
export HUGGINGFACE_HUB_CACHE="$HF_CACHE/hub"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-8}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-8}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-8}"

command=(
    "$PYTHON" -m heretic.engineering_direct
    --model "$MODEL" --config-template "$CONFIG" --run-root "$RUN_ROOT"
    --fit-samples "${ARA_FIT_SAMPLES:-8}"
    --monitor-samples "${ARA_MONITOR_SAMPLES:-8}"
    --development-samples "${ARA_DEVELOPMENT_SAMPLES:-20}"
    --trials "${ARA_TRIALS:-1}" --seed "${ARA_SEED:-42}"
    --max-hours "${ARA_MAX_HOURS:-8}"
)
[[ -z "${ARA_RUN_DIR:-}" ]] || command+=(--run-dir "$ARA_RUN_DIR")
command+=("${extra_args[@]}")
printf 'Qwen3-1.7B / latest ARA，物理 GPU：%s\n实际命令：\n  ' "$GPU"
printf '%q ' "${command[@]}"
printf '\n'
exec "${command[@]}"
