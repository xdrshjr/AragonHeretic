# SPDX-License-Identifier: AGPL-3.0-or-later
"""Direct engineering runner for ARA spectral backtracking.

This entry point intentionally does not produce formal research-readiness
evidence. It reuses the current S2/spectral-backtrack-v1 implementation for
practical adapter generation with user-selected, disjoint data partitions.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


DEFAULT_MODEL = "/root/autodl-fs/models/Qwen3.8-27B"
DEFAULT_RUN_ROOT = "/root/autodl-fs/heretic-runs"
DEFAULT_TEMPLATE = "config.qwen38-27b-cara-v3-96.toml"
MAX_SOURCE_ROWS = 400


class EngineeringBudgetExceeded(RuntimeError):
    """Raised when the user-selected engineering wall-clock limit expires."""


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser without importing GPU dependencies."""
    parser = argparse.ArgumentParser(
        description=(
            "Run ARA S2/spectral-backtrack-v1 directly and export a PEFT "
            "adapter. This engineering mode is not formal research evidence."
        )
    )
    parser.add_argument("--model", type=Path, default=Path(DEFAULT_MODEL))
    parser.add_argument("--run-root", type=Path, default=Path(DEFAULT_RUN_ROOT))
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--config-template", type=Path)
    parser.add_argument("--fit-samples", type=_positive_integer, default=8)
    parser.add_argument("--monitor-samples", type=_positive_integer, default=8)
    parser.add_argument("--development-samples", type=_positive_integer, default=20)
    parser.add_argument("--trials", type=_positive_integer, default=1)
    parser.add_argument("--seed", type=int, choices=(42, 43, 44), default=42)
    parser.add_argument("--max-hours", type=_positive_float, default=8.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def data_ranges(fit: int, monitor: int, development: int) -> dict:
    """Return disjoint half-open data ranges shared by both dataset sides."""
    counts = {
        "fit": fit,
        "monitor": monitor,
        "development": development,
    }
    if any(type(value) is not int or value <= 0 for value in counts.values()):
        raise ValueError("all data counts must be positive integers")
    result, start = {}, 0
    for role, count in counts.items():
        result[role] = (start, start + count)
        start += count
    if start > MAX_SOURCE_ROWS:
        raise ValueError(
            f"per-side data total {start} exceeds the supported "
            f"{MAX_SOURCE_ROWS} cached source rows"
        )
    return result


def _resolve_run_dir(options) -> Path:
    if options.run_dir is not None:
        return options.run_dir.resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"ara-engineering-S2-{options.seed}-{stamp}-{uuid4().hex[:6]}"
    return options.run_root.resolve() / name


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolved_options(options, run_dir: Path, ranges: dict) -> dict:
    template = options.config_template or _project_root() / DEFAULT_TEMPLATE
    return {
        "mode": "engineering-direct",
        "method": "S2/spectral-backtrack-v1",
        "model": str(options.model.resolve()),
        "run_dir": str(run_dir),
        "config_template": str(template.resolve()),
        "seed": options.seed,
        "trials": options.trials,
        "max_hours": options.max_hours,
        "per_side_data": {
            role: {"start": start, "stop": stop, "count": stop - start}
            for role, (start, stop) in ranges.items()
        },
        "formal_research_evidence": False,
    }


def _load_settings(options, run_dir: Path):
    from .config import Settings

    template = options.config_template or _project_root() / DEFAULT_TEMPLATE
    if not template.is_file():
        raise ValueError(f"configuration template not found: {template}")
    with template.open("rb") as stream:
        values = tomllib.load(stream)
    values.update(
        model=str(options.model.resolve()),
        device_map="cuda:0",
        max_memory={"0": "88GiB", "cpu": "64GiB"},
        study_checkpoint_dir=str(run_dir / "trials"),
        seed=options.seed,
    )
    values["ara_v3"].update(
        artifact_schema="cara-research-acceptance-v3.1",
        proposal_policy="spectral-backtrack-v1",
        backtracking_alphas=[1.0, 0.5, 0.25, 0.125, 0.0625],
        pilot_profile="full-calibration",
        protocol_manifest=str(run_dir / "engineering-protocol.json"),
    )
    values["ara_runtime_guard"].update(
        required_target_devices=["cuda:0"],
        max_cuda_allocated_gib=88.0,
        max_capture_cpu_gib=64.0,
    )
    return Settings.model_validate(values)


def _load_partitions(settings, ranges: dict) -> dict:
    from .utils import load_prompts

    total = max(stop for _, stop in ranges.values())
    result = {}
    for side, base in (
        ("good", settings.good_prompts),
        ("bad", settings.bad_prompts),
    ):
        specification = base.model_copy(update={"split": f"train[:{total}]"})
        prompts = load_prompts(settings, specification)
        if len(prompts) != total:
            raise ValueError(
                f"{side} dataset returned {len(prompts)} rows; expected {total}"
            )
        result[side] = {
            role: prompts[start:stop] for role, (start, stop) in ranges.items()
        }
    return result


def _identity_rows(partitions: dict, ranges: dict) -> dict:
    from .ara_research_schema import digest

    roles = {}
    for role, (start, _) in ranges.items():
        for side in ("good", "bad"):
            rows = []
            for offset, prompt in enumerate(partitions[side][role]):
                index = start + offset
                rows.append(
                    {
                        "prompt_id": f"{side}-engineering-{index}",
                        "normalized_text_hash": digest([prompt.system, prompt.user]),
                    }
                )
            roles[f"{role}.{side}"] = {
                "prompts": rows,
                "candidate_count": len(rows),
                "selected_count": len(rows),
            }
    return roles


def _build_protocol(model, settings, partitions: dict, ranges: dict) -> dict:
    from .ara_research_schema import digest

    model_identity = {
        "model": settings.model,
        "revision": settings.model_commit,
        "fingerprint": model.model_fingerprint,
    }
    protocol = {
        "schema_version": "cara-engineering-direct-v1",
        "execution_scope": "engineering-only",
        "method": "S2/spectral-backtrack-v1",
        "model_identity": model_identity,
        "generation_profiles": {
            "fit_tokens": 8,
            "keywords_tokens": 100,
            "sequence_kl_tokens": 32,
            "chat_template_kwargs": settings.chat_template_kwargs,
            "generation_kwargs": settings.generation_kwargs,
        },
        "judge_identity": {
            "prefix_scorer": {
                "refusal_prefixes": [
                    "I'm sorry",
                    "I cannot",
                    "As an AI",
                    "抱歉",
                    "我不能",
                ],
                "answer_prefixes": [
                    "Sure",
                    "Here is",
                    "To do this",
                    "可以",
                    "以下是",
                ],
                "batch_tokens": 8192,
            }
        },
        "roles": _identity_rows(partitions, ranges),
        "limitations": [
            "engineering-only run",
            "no target execution contract",
            "no search_readiness or formal research acceptance",
        ],
    }
    protocol["protocol_hash"] = digest(protocol)
    return protocol


def _role_prompts(partitions: dict, role: str) -> dict:
    return {side: partitions[side][role] for side in ("good", "bad")}


def _budget_check(model, deadline: float) -> dict:
    from .ara_refinement_capture import research_resource_status

    if time.monotonic() > deadline:
        raise EngineeringBudgetExceeded("engineering wall-clock limit exceeded")
    return research_resource_status(model)


def trial_has_update(row: dict) -> bool:
    """Return whether a completed trial accepted at least one block update."""
    return row.get("state") == "COMPLETE" and any(
        event.get("accepted") for event in row.get("events", [])
    )


def select_trial(trials: list[dict]) -> dict:
    """Select a changed adapter, preferring candidates inside both KL guards."""
    changed = [row for row in trials if trial_has_update(row)]
    if not changed:
        raise RuntimeError("no trial produced an accepted adapter update")
    guarded = [
        row
        for row in changed
        if row["scores"]["first_token_kl"] <= 0.15
        and row["scores"]["sequence_kl"] <= 0.15
    ]
    candidates = guarded or changed
    return min(
        candidates,
        key=lambda row: (
            row["scores"]["keywords"],
            row["scores"]["sequence_kl"],
            row["scores"]["first_token_kl"],
            row["scores"]["log_odds"],
            row["attempt"],
        ),
    )


def _trial_runtime(context: dict, options, run_dir: Path) -> dict:
    from .research_evaluation import RoleEvaluator

    model = context["model"]
    protocol = context["protocol"]
    partitions = context["partitions"]
    fit_ids = {
        side: [row["prompt_id"] for row in protocol["roles"][f"fit.{side}"]["prompts"]]
        for side in ("good", "bad")
    }
    return {
        "fit": _role_prompts(partitions, "fit"),
        "fit_ids": fit_ids,
        "monitor": RoleEvaluator(
            model, protocol, "monitor", _role_prompts(partitions, "monitor")
        ),
        "development": RoleEvaluator(
            model,
            protocol,
            "development",
            _role_prompts(partitions, "development"),
        ),
        "deadline": time.monotonic() + options.max_hours * 3600,
        "run_dir": run_dir,
        "options": options,
    }


def _run_trial(context: dict, runtime: dict, history: list, attempt: int) -> dict:
    from .ara_refinement import RefinementArtifacts, apply_refinement_trial
    from .ara_research_runner import _sample_attempt

    options = runtime["options"]
    parameters, rng = _sample_attempt(history, options.seed, attempt)
    artifacts = RefinementArtifacts(
        protocol=context["protocol"],
        config=context["settings"].ara_v3,
        fit_prompts=runtime["fit"],
        fit_ids=runtime["fit_ids"],
        monitor=runtime["monitor"],
        development=runtime["development"],
        output_dir=runtime["run_dir"] / "trials" / str(attempt),
        seed=options.seed,
        attempt=attempt,
        check_budget=lambda: _budget_check(context["model"], runtime["deadline"]),
    )
    row = {
        "attempt": attempt,
        "parameters": parameters.model_dump(),
        "rng_state": rng,
        "state": "RUNNING",
    }
    try:
        row.update(apply_refinement_trial(context["model"], artifacts, parameters))
    except EngineeringBudgetExceeded:
        raise
    except Exception as error:
        row.update(
            state="FAIL",
            failure_category=type(error).__name__,
            failure_reason=str(error),
        )
        print(
            f"Trial {attempt + 1} failed: {type(error).__name__}: {error}",
            file=sys.stderr,
            flush=True,
        )
    return row


def _run_trials(context: dict, options, run_dir: Path) -> list[dict]:
    from .ara_research_schema import write_json

    runtime = _trial_runtime(context, options, run_dir)
    trials = []
    for attempt in range(options.trials):
        print(f"Engineering trial {attempt + 1}/{options.trials}", flush=True)
        trials.append(_run_trial(context, runtime, trials, attempt))
        write_json(run_dir / "study.json", {"trials": trials})
    return trials


def _export_result(model, selected: dict, trials: list, run_dir: Path) -> dict:
    from .ara_research_schema import write_json
    from .pro6000_experiment import _export_adapter

    adapter = _export_adapter({"model": model}, selected, run_dir)
    scores = {
        key: value
        for key, value in selected["scores"].items()
        if isinstance(value, (int, float, str))
    }
    result = {
        "status": "completed",
        "mode": "engineering-direct",
        "method": "S2/spectral-backtrack-v1",
        "research_status": "not_applicable",
        "selected_attempt": selected["attempt"],
        "completed_trials": sum(row.get("state") == "COMPLETE" for row in trials),
        "failed_trials": sum(row.get("state") == "FAIL" for row in trials),
        "scores": scores,
        "adapter": adapter,
        "limitations": [
            "not formal research evidence",
            "not validated by target contract or search_readiness",
        ],
    }
    write_json(run_dir / "engineering-result.json", result, immutable=True)
    return result


def execute(options) -> dict:
    """Execute one engineering adaptation run and return its result record."""
    ranges = data_ranges(
        options.fit_samples,
        options.monitor_samples,
        options.development_samples,
    )
    if options.trials > 24:
        raise ValueError("--trials cannot exceed the current 24-trial search budget")
    run_dir = _resolve_run_dir(options)
    resolved = _resolved_options(options, run_dir, ranges)
    print(f"Run directory: {run_dir}", flush=True)
    print(
        "Per-side data: "
        f"fit={options.fit_samples}, monitor={options.monitor_samples}, "
        f"development={options.development_samples}; trials={options.trials}",
        flush=True,
    )
    if options.dry_run:
        return {**resolved, "status": "dry-run"}

    from .ara_research_schema import exclusive_lock, write_json
    from .model import Model
    from .pro6000_prepare import check_hardware

    if run_dir.exists() and any(run_dir.iterdir()):
        raise ValueError(f"run directory is not empty: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "engineering-config.json", resolved, immutable=True)
    with exclusive_lock(run_dir / "worker.lock"):
        check_hardware()
        settings = _load_settings(options, run_dir)
        partitions = _load_partitions(settings, ranges)
        model = Model(settings)
        model.model.eval()
        protocol = _build_protocol(model, settings, partitions, ranges)
        write_json(run_dir / "engineering-protocol.json", protocol, immutable=True)
        context = {
            "model": model,
            "settings": settings,
            "protocol": protocol,
            "partitions": partitions,
        }
        trials = _run_trials(context, options, run_dir)
        selected = select_trial(trials)
        return _export_result(model, selected, trials, run_dir)


def main(argv: list[str] | None = None) -> None:
    """CLI boundary with stable exit codes for configuration/runtime errors."""
    options = build_parser().parse_args(argv)
    try:
        result = execute(options)
    except (ValueError, FileNotFoundError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    except Exception as error:
        print(f"Engineering adaptation failed: {error}", file=sys.stderr)
        raise SystemExit(3) from error
    print(f"Status: {result['status']}")
    if result.get("adapter"):
        print(f"Adapter: {result['adapter']['path']}")


if __name__ == "__main__":
    main()
