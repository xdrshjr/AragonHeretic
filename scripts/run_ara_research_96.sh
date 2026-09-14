#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# 新运行使用 v3.1 冻结策略及阶段放行；所有阶段透传真实退出码。
# R1/R2 配置必须注册 stage_execution_id；不设置 CUDA_VISIBLE_DEVICES。
set -euo pipefail

research_python="${HERETIC_RESEARCH_PYTHON:-/home/xdrshjr/miniconda3/envs/aragon-heretic/bin/python}"
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$project_root"
export PYTHONPATH="$project_root/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1

arguments=("$@")
run_dir=""
phase=""
while (($#)); do
    case "$1" in
        --run-dir) run_dir="${2:?缺少运行目录}"; shift 2 ;;
        --phase) phase="${2:?缺少阶段}"; shift 2 ;;
        *) shift ;;
    esac
done
if [[ -z "$run_dir" || -z "$phase" ]]; then
    printf '%s\n' '必须指定 --config、--run-dir 和 --phase。' >&2
    exit 2
fi
mkdir --parents -- "$run_dir"
exec 9>"$run_dir/wrapper.lock"
if ! flock --nonblock 9; then
    printf '%s\n' '该运行目录已由另一 worker 占用。' >&2
    exit 3
fi
printf 'ARA v3 阶段：%s；运行目录：%s\n' "$phase" "$run_dir"
set +e
"$research_python" -m heretic.ara_research_runner "${arguments[@]}"
exit_code=$?
set -e
if [[ "$exit_code" -eq 0 && ! -f "$run_dir/$phase-result.json" ]]; then
    printf '%s\n' '阶段返回成功但结果文件缺失。' >&2
    exit_code=3
fi
printf '{"phase":"%s","exit_code":%d}\n' "$phase" "$exit_code" >"$run_dir/.exit.tmp"
mv -- "$run_dir/.exit.tmp" "$run_dir/exit.json"
exit "$exit_code"
