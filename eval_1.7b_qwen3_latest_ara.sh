#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
set -Eeuo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DATA_ROOT="$HOME"
DEFAULT_ENV="$HOME/miniconda3/envs/aragon-heretic"
DEFAULT_CACHE="$HOME/.cache/huggingface"
if [[ -d /root/autodl-fs && -r /root/autodl-fs ]]; then
    DATA_ROOT=/root/autodl-fs
    DEFAULT_ENV="$DATA_ROOT/conda-envs/heretic-dsv4"
    DEFAULT_CACHE="$DATA_ROOT/hf-cache"
fi

# 用户配置区：修改 adapter 地址、每侧评估数量和 GPU 即可。
DEFAULT_ADAPTER="$DATA_ROOT/heretic-runs/qwen3-1.7b"
DEFAULT_ADAPTER+="/ara-engineering-S2-42-20260917T053956Z/artifacts/best-adapter"
ADAPTER="${ARA_EVAL_ADAPTER:-$DEFAULT_ADAPTER}"
EVAL_SAMPLES="${ARA_EVAL_SAMPLES:-20}"  # 每侧 N 条，共 2*N 条
START_INDEX="${ARA_EVAL_START_INDEX:-16}"  # auto 跳过来源训练运行使用的行
GPU="${ARA_GPU:-${CUDA_VISIBLE_DEVICES:-1}}"
MODEL="${ARA_MODEL:-$DATA_ROOT/models/Qwen3-1.7B}"
CONDA_ENV="${ARA_CONDA_ENV:-${CONDA_PREFIX:-$DEFAULT_ENV}}"
PYTHON="${ARA_PYTHON:-}"
HF_CACHE="${ARA_HF_CACHE:-${HF_HOME:-$DEFAULT_CACHE}}"
OUTPUT_ROOT="${ARA_EVAL_OUTPUT_ROOT:-$PROJECT_ROOT/eval-runs/qwen3-1.7b}"
CONFIG="${ARA_CONFIG_TEMPLATE:-$PROJECT_ROOT/config.qwen3-1.7b-ara-v3.toml}"
extra_args=()

usage() {
    cat <<'EOF'
用法：./eval_1.7b_qwen3_latest_ara.sh [选项]

独立加载已导出的 ARA Adapter，与原模型在相同数据上比较。
可直接编辑脚本顶部 ADAPTER、EVAL_SAMPLES、START_INDEX、GPU。

  --adapter PATH          Adapter 目录，含 adapter_config.json 和 safetensors
  --eval-samples N        每侧评估 N 条，共 N 条 good + N 条 bad，默认 20
  --start-index auto|N    数据起点（从 0 开始）；默认 auto
                         auto 从来源运行全部 fit/monitor/development 之后取样
                         若 Adapter 已移动且无来源配置，需显式指定数字
                         起点 + 每侧数量不能超过 400
  --model PATH            本地基础模型目录
  --gpu INDEX|UUID        单张物理 GPU，默认 1
  --output-root PATH      评估结果父目录，每次创建新子目录
  --output-dir PATH       指定空的评估结果目录
  --python PATH           Python 可执行文件
  --conda-env PATH        Conda 环境目录
  --hf-cache PATH         Hugging Face 缓存
  --config-template PATH  模型配置模板
  --seed 42|43|44         随机种子，默认 42
  --offline               仅使用本地缓存
  --dry-run               验证路径和数据范围，不加载模型、不创建输出目录
  -h, --help              显示帮助

示例：
  ./eval_1.7b_qwen3_latest_ara.sh --offline
  ./eval_1.7b_qwen3_latest_ara.sh --adapter /path/best-adapter --eval-samples 100
  ./eval_1.7b_qwen3_latest_ara.sh --eval-samples 20 --start-index 16 --offline

最后一个示例复核默认训练时的 development 数据，不是独立留出集。
结果：eval-result.json（汇总）、eval-evidence.json（逐题原模型/Adapter 回答）、
eval-protocol.json（数据身份及生成配置）。关键词率越低表示拒答标记越少。
EOF
}
die() { printf '错误：%s\n' "$*" >&2; exit 2; }
need_value() { [[ $# -ge 2 && -n "${2:-}" ]] || die "$1 缺少参数值"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --adapter) need_value "$@"; ADAPTER="$2"; shift 2 ;;
        --eval-samples) need_value "$@"; EVAL_SAMPLES="$2"; shift 2 ;;
        --start-index) need_value "$@"; START_INDEX="$2"; shift 2 ;;
        --model) need_value "$@"; MODEL="$2"; shift 2 ;;
        --gpu) need_value "$@"; GPU="$2"; shift 2 ;;
        --python) need_value "$@"; PYTHON="$2"; shift 2 ;;
        --conda-env)
            need_value "$@"; CONDA_ENV="$2"
            PYTHON="$CONDA_ENV/bin/python"; shift 2 ;;
        --hf-cache) need_value "$@"; HF_CACHE="$2"; shift 2 ;;
        --output-root) need_value "$@"; OUTPUT_ROOT="$2"; shift 2 ;;
        --config-template) need_value "$@"; CONFIG="$2"; shift 2 ;;
        --output-dir|--seed)
            need_value "$@"; extra_args+=("$1" "$2"); shift 2 ;;
        --offline)
            export HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 TRANSFORMERS_OFFLINE=1
            shift ;;
        --dry-run) extra_args+=("$1"); shift ;;
        *) die "未知参数：$1（使用 --help 查看帮助）" ;;
    esac
done

[[ -n "$GPU" && "$GPU" != *,* && "$GPU" != "-1" ]] || die "请指定单张 GPU"
PYTHON="${PYTHON:-$CONDA_ENV/bin/python}"
command -v "$PYTHON" >/dev/null 2>&1 || die "未找到 Python：$PYTHON"
export CUDA_VISIBLE_DEVICES="$GPU"
export PYTHONPATH="$PROJECT_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1 PYTHONNOUSERSITE=1 TOKENIZERS_PARALLELISM=false
export HF_HOME="$HF_CACHE" HF_HUB_CACHE="$HF_CACHE/hub"
export HUGGINGFACE_HUB_CACHE="$HF_CACHE/hub"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-8}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-8}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-8}"

command=(
    "$PYTHON" -m heretic.engineering_eval
    --model "$MODEL" --adapter "$ADAPTER" --config-template "$CONFIG"
    --eval-samples "$EVAL_SAMPLES" --start-index "$START_INDEX"
    --output-root "$OUTPUT_ROOT" "${extra_args[@]}"
)
printf 'Qwen3-1.7B / ARA eval，物理 GPU：%s\n实际命令：\n  ' "$GPU"
printf '%q ' "${command[@]}"
printf '\n'
exec "${command[@]}"
