#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# One-command entry point for the latest ARA v3.1 spectral-backtrack method.
#
# The research protocol intentionally freezes the admissible sample layouts.
# This wrapper exposes the layouts as named profiles and rejects unsupported
# counts before any GPU work starts, instead of silently ignoring them.

set -Eeuo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<'EOF'
Usage:
  bash run_latest_ara.sh [options]

Run the latest ARA v3.1 spectral-backtrack adaptation. The default `full`
profile launches the formal Pro6000 experiment and exports the selected adapter.

Profiles and frozen per-side data layouts:
  full   fit candidates=192, fit selected=96, monitor=64,
         mechanism-development=44, development=100 (default)
  smoke  fit candidates=8, fit selected=8, monitor=8,
         mechanism-development=44, development=100

Core options:
  --profile NAME                 full or smoke (default: full)
  --model PATH                   full-profile model directory; the smoke model
                                 is read from its frozen TOML configuration
  --conda-env PATH               Conda environment directory
  --hf-cache PATH                Hugging Face cache directory
  --run-root PATH                parent directory for experiment runs
  --seed N                       42, 43, or 44 (default: 42)
  --hours N                      full-run wall-clock budget (default: 48)

Data options (per refusal/compliance side):
  --fit-candidates N             fit-pool candidate count
  --fit-samples N                selected fit count
  --monitor-samples N            held-out monitor count
  --mechanism-samples N          mechanism-development count
  --development-samples N        development count

Full-profile evidence:
  --target-execution-contract P  frozen target execution contract JSON
  --readiness-evidence P         search_readiness evidence JSON
  --pilot-record PATH            completed pilot run directory
  --prepare-only                 prepare and validate, but do not launch worker
  --resume RUN_DIR               resume an existing full run
  --status RUN_DIR               show status for an existing full run

Smoke-profile inputs:
  --config PATH                  frozen smoke TOML configuration
  --run-dir PATH                 output directory; generated under --run-root
                                 when omitted

Other:
  --dry-run                      print the resolved command without executing it
  -h, --help                     show this help

Environment-variable equivalents:
  ARA_PROFILE, ARA_MODEL, ARA_CONDA_ENV, ARA_HF_CACHE, ARA_RUN_ROOT,
  ARA_SEED, ARA_HOURS, ARA_TARGET_EXECUTION_CONTRACT,
  ARA_READINESS_EVIDENCE, ARA_PILOT_RECORD, ARA_SMOKE_CONFIG

Examples:
  bash run_latest_ara.sh \
    --target-execution-contract /path/target-execution-contract.json \
    --readiness-evidence /path/search-readiness.json

  bash run_latest_ara.sh --profile smoke \
    --config /path/ara-v31-smoke.toml --fit-samples 8 --monitor-samples 8

Notes:
  * `full` is the complete adaptation path and exports a PEFT adapter.
  * `smoke` is a quick method-validation path and writes factors/trial results;
    it is not a replacement for the formal full adapter export.
  * Counts outside the two frozen profiles are not accepted by the latest
    research protocol. To change them, regenerate and re-review the protocol
    artifacts instead of overriding values only in this shell script.
EOF
}

die() {
    printf 'Error: %s\n' "$*" >&2
    exit 2
}

need_value() {
    [[ $# -ge 2 && -n "${2:-}" ]] || die "$1 requires a value"
}

positive_integer() {
    [[ "$2" =~ ^[1-9][0-9]*$ ]] || die "$1 must be a positive integer (got: $2)"
}

print_command() {
    printf 'Resolved command:\n  '
    printf '%q ' "$@"
    printf '\n'
}

profile="${ARA_PROFILE:-full}"
model="${ARA_MODEL:-/root/autodl-fs/models/Qwen3.8-27B}"
conda_env="${ARA_CONDA_ENV:-/root/autodl-fs/conda-envs/heretic-dsv4}"
hf_cache="${ARA_HF_CACHE:-/root/autodl-fs/hf-cache}"
run_root="${ARA_RUN_ROOT:-/root/autodl-fs/heretic-runs}"
seed="${ARA_SEED:-42}"
hours="${ARA_HOURS:-48}"
target_contract="${ARA_TARGET_EXECUTION_CONTRACT:-}"
readiness_evidence="${ARA_READINESS_EVIDENCE:-}"
pilot_record="${ARA_PILOT_RECORD:-/root/autodl-fs/heretic-runs/ara-v3-pro6000-smoke-20260911}"
smoke_config="${ARA_SMOKE_CONFIG:-}"
smoke_run_dir=""
resume_run=""
status_run=""
prepare_only=0
dry_run=0

# Leave these unset until the profile is known so each profile can supply its
# own defaults while still allowing explicit command-line validation.
fit_candidates=""
fit_samples=""
monitor_samples=""
mechanism_samples=""
development_samples=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile)
            need_value "$@"; profile="$2"; shift 2 ;;
        --model)
            need_value "$@"; model="$2"; shift 2 ;;
        --conda-env)
            need_value "$@"; conda_env="$2"; shift 2 ;;
        --hf-cache)
            need_value "$@"; hf_cache="$2"; shift 2 ;;
        --run-root)
            need_value "$@"; run_root="$2"; shift 2 ;;
        --seed)
            need_value "$@"; seed="$2"; shift 2 ;;
        --hours)
            need_value "$@"; hours="$2"; shift 2 ;;
        --fit-candidates)
            need_value "$@"; fit_candidates="$2"; shift 2 ;;
        --fit-samples)
            need_value "$@"; fit_samples="$2"; shift 2 ;;
        --monitor-samples)
            need_value "$@"; monitor_samples="$2"; shift 2 ;;
        --mechanism-samples)
            need_value "$@"; mechanism_samples="$2"; shift 2 ;;
        --development-samples)
            need_value "$@"; development_samples="$2"; shift 2 ;;
        --target-execution-contract)
            need_value "$@"; target_contract="$2"; shift 2 ;;
        --readiness-evidence)
            need_value "$@"; readiness_evidence="$2"; shift 2 ;;
        --pilot-record)
            need_value "$@"; pilot_record="$2"; shift 2 ;;
        --config)
            need_value "$@"; smoke_config="$2"; shift 2 ;;
        --run-dir)
            need_value "$@"; smoke_run_dir="$2"; shift 2 ;;
        --resume)
            need_value "$@"; resume_run="$2"; shift 2 ;;
        --status)
            need_value "$@"; status_run="$2"; shift 2 ;;
        --prepare-only)
            prepare_only=1; shift ;;
        --dry-run)
            dry_run=1; shift ;;
        -h|--help)
            usage; exit 0 ;;
        --)
            shift
            [[ $# -eq 0 ]] || die "unexpected positional arguments: $*"
            ;;
        *)
            die "unknown option: $1 (use --help)" ;;
    esac
done

case "$profile" in
    full)
        expected_fit_candidates=192
        expected_fit_samples=96
        expected_monitor_samples=64
        ;;
    smoke)
        expected_fit_candidates=8
        expected_fit_samples=8
        expected_monitor_samples=8
        ;;
    *)
        die "--profile must be 'full' or 'smoke' (got: $profile)" ;;
esac
expected_mechanism_samples=44
expected_development_samples=100

fit_candidates="${fit_candidates:-$expected_fit_candidates}"
fit_samples="${fit_samples:-$expected_fit_samples}"
monitor_samples="${monitor_samples:-$expected_monitor_samples}"
mechanism_samples="${mechanism_samples:-$expected_mechanism_samples}"
development_samples="${development_samples:-$expected_development_samples}"

positive_integer --fit-candidates "$fit_candidates"
positive_integer --fit-samples "$fit_samples"
positive_integer --monitor-samples "$monitor_samples"
positive_integer --mechanism-samples "$mechanism_samples"
positive_integer --development-samples "$development_samples"

[[ "$fit_candidates" == "$expected_fit_candidates" ]] ||
    die "$profile profile requires --fit-candidates $expected_fit_candidates (got: $fit_candidates)"
[[ "$fit_samples" == "$expected_fit_samples" ]] ||
    die "$profile profile requires --fit-samples $expected_fit_samples (got: $fit_samples)"
[[ "$monitor_samples" == "$expected_monitor_samples" ]] ||
    die "$profile profile requires --monitor-samples $expected_monitor_samples (got: $monitor_samples)"
[[ "$mechanism_samples" == "$expected_mechanism_samples" ]] ||
    die "$profile profile requires --mechanism-samples $expected_mechanism_samples (got: $mechanism_samples)"
[[ "$development_samples" == "$expected_development_samples" ]] ||
    die "$profile profile requires --development-samples $expected_development_samples (got: $development_samples)"

[[ "$seed" == "42" || "$seed" == "43" || "$seed" == "44" ]] ||
    die "--seed must be 42, 43, or 44 (got: $seed)"
[[ "$hours" =~ ^(([1-9][0-9]*)(\.[0-9]+)?|0\.[0-9]*[1-9][0-9]*)$ ]] ||
    die "--hours must be a positive number (got: $hours)"

printf 'ARA method: spectral-backtrack-v1 (S2 sequential refresh, 2 sweeps)\n'
printf 'Profile: %s\n' "$profile"
printf 'Per-side data: fit candidates=%s, fit selected=%s, monitor=%s, mechanism-development=%s, development=%s\n' \
    "$fit_candidates" "$fit_samples" "$monitor_samples" "$mechanism_samples" "$development_samples"

export HERETIC_RESEARCH_PYTHON="$conda_env/bin/python"
export HF_HOME="$hf_cache"
export HUGGINGFACE_HUB_CACHE="$hf_cache/hub"
export TRANSFORMERS_CACHE="$hf_cache/hub"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"

if [[ "$profile" == "full" ]]; then
    [[ -z "$smoke_config" && -z "$smoke_run_dir" ]] ||
        die "--config/--run-dir are only valid with --profile smoke"
    [[ -z "$resume_run" || -z "$status_run" ]] ||
        die "--resume and --status cannot be used together"
    [[ -z "$resume_run$status_run" || "$prepare_only" -eq 0 ]] ||
        die "--prepare-only cannot be combined with --resume or --status"

    full_launcher="$PROJECT_ROOT/scripts/run_qwen38_27b_ara_v3_pro6000.sh"
    command=(bash "$full_launcher")

    if [[ -n "$resume_run" ]]; then
        command+=(--resume "$resume_run")
    elif [[ -n "$status_run" ]]; then
        command+=(--status "$status_run")
    else
        [[ -n "$target_contract" ]] ||
            die "full profile requires --target-execution-contract (or ARA_TARGET_EXECUTION_CONTRACT)"
        [[ -n "$readiness_evidence" ]] ||
            die "full profile requires --readiness-evidence (or ARA_READINESS_EVIDENCE)"
        command+=(
            --target-execution-contract "$target_contract"
            --readiness-evidence "$readiness_evidence"
            --pilot-record "$pilot_record"
            --model "$model"
            --run-root "$run_root"
            --seed "$seed"
            --hours "$hours"
        )
        [[ "$prepare_only" -eq 0 ]] || command+=(--prepare-only)
    fi

    print_command "${command[@]}"
    [[ "$dry_run" -eq 0 ]] || exit 0

    [[ -x "$HERETIC_RESEARCH_PYTHON" ]] ||
        die "Python is not executable: $HERETIC_RESEARCH_PYTHON"
    [[ -f "$full_launcher" ]] || die "latest full launcher not found: $full_launcher"
    if [[ -z "$resume_run$status_run" ]]; then
        [[ -d "$model" ]] || die "model directory not found: $model"
        [[ -f "$target_contract" ]] || die "target execution contract not found: $target_contract"
        [[ -f "$readiness_evidence" ]] || die "readiness evidence not found: $readiness_evidence"
        [[ -d "$pilot_record" ]] || die "pilot record directory not found: $pilot_record"
    fi

    exec "${command[@]}"
fi

[[ -z "$resume_run$status_run" ]] || die "--resume/--status are only valid with --profile full"
[[ "$prepare_only" -eq 0 ]] || die "--prepare-only is only valid with --profile full"
[[ -n "$smoke_config" ]] || die "smoke profile requires --config (or ARA_SMOKE_CONFIG)"

if [[ -z "$smoke_run_dir" ]]; then
    smoke_run_dir="$run_root/ara-v31-smoke-$(date -u +%Y%m%dT%H%M%SZ)"
fi

smoke_launcher="$PROJECT_ROOT/scripts/run_ara_research_96.sh"
command=(
    bash "$smoke_launcher"
    --config "$smoke_config"
    --run-dir "$smoke_run_dir"
    --phase pilot
)

print_command "${command[@]}"
[[ "$dry_run" -eq 0 ]] || exit 0

[[ -x "$HERETIC_RESEARCH_PYTHON" ]] ||
    die "Python is not executable: $HERETIC_RESEARCH_PYTHON"
[[ -f "$smoke_launcher" ]] || die "smoke launcher not found: $smoke_launcher"
[[ -f "$smoke_config" ]] || die "smoke configuration not found: $smoke_config"

exec "${command[@]}"
