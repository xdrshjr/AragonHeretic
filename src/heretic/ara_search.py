# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic trajectory-v2 search, constraints, and study summaries."""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from optuna import Study, Trial
from optuna.trial import FrozenTrial, TrialState

from .ara_config import (
    ARA_PARAMETER_NAMES,
    ARASeedTrial,
    ARASearchSpace,
    AcceptanceGate,
)

Trials = Sequence[FrozenTrial]
TrialSource = Trials | Study

SEARCH_SPACE_VERSION = "trajectory-v2-eight-dimensional-v1"
SAMPLER_PROTOCOL = "anchors-8_random-24_tpe-88_per-attempt-sha256-v1"
CANDIDATE_CONSTRAINTS_KEY = "candidate_constraints"
TERMINAL_STATES = frozenset({TrialState.COMPLETE, TrialState.PRUNED, TrialState.FAIL})
RUNTIME_FAILURE_CATEGORIES = frozenset({"oom", "non_finite", "device", "interrupted"})


@dataclass(frozen=True)
class ARATrajectoryParameters:
    """Resolved layer range and eight sampled trajectory coordinates."""

    layer_start_fraction: float
    layer_span_fraction: float
    start_layer_index: int
    end_layer_index: int
    attn_strength: float
    mlp_strength: float
    push_weight: float
    margin: float
    attn_deployment_gain: float
    mlp_deployment_gain: float


@dataclass(frozen=True)
class ARASamplingContext:
    """Immutable context required to resolve fractional layer coordinates."""

    layer_count: int


@dataclass(frozen=True)
class TrialFailureRecord:
    """Machine-readable failure classification for one attempt."""

    trial_number: int
    category: str
    stage: str
    exception_type: str
    message: str
    is_runtime_failure: bool
    module_key: str | None = None


@dataclass(frozen=True)
class StudySummary:
    """Finite diagnostics describing all terminal and non-terminal attempts."""

    state_counts: Mapping[str, int]
    runtime_failure_count: int
    runtime_failure_rate: float
    runtime_failure_categories: Mapping[str, int]
    score_statistics: Mapping[str, Mapping[str, float | None]]
    constraint_statistics: Mapping[str, Mapping[str, float | None]]
    best_keyword_trial: int | None
    best_objective_trial: int | None
    pareto_trial_ids: tuple[int, ...]
    parameter_boundary_distances: Mapping[str, Mapping[str, float | None]]


def derive_sampler_seed(seed: int, phase: str, trial_number: int) -> int:
    """Derive a stable unsigned 32-bit seed for one attempt."""
    payload = f"trajectory-v2\0{seed}\0{phase}\0{trial_number}".encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:4], "big")


def format_trial_parameters(trial: Trial | FrozenTrial) -> dict[str, str]:
    """Format directional, point-v1, or trajectory-v2 resolved parameters."""
    if trial.user_attrs.get("method") == "ara":
        payload = trial.user_attrs["ara_parameters"]
        if trial.user_attrs.get("objective_version") == "trajectory-v2":
            values = payload["payload"]
            return {
                name: f"{value:.4f}" if isinstance(value, float) else str(value)
                for name, value in values.items()
            }
        parameters = {
            "layer_start": str(payload["start_layer_index"]),
            "layer_end": str(payload["end_layer_index"]),
        }
        for component, values in payload["components"].items():
            for name, value in values.items():
                parameters[f"{component}.{name}"] = f"{value:.4f}"
        return parameters
    direction_index = trial.user_attrs["direction_index"]
    parameters = {
        "direction_index": (
            "per layer" if direction_index is None else f"{direction_index:.2f}"
        )
    }
    for component, values in trial.user_attrs["parameters"].items():
        for name, value in values.items():
            parameters[f"{component}.{name}"] = f"{value:.2f}"
    return parameters


def _suggest_coordinate(
    trial: Trial,
    name: str,
    search_space: ARASearchSpace,
) -> float:
    lower, upper = getattr(search_space, name)
    use_log = name not in {"layer_start", "layer_span"}
    return trial.suggest_float(name, lower, upper, log=use_log)


def sample_v2_parameters(
    trial: Trial,
    context: ARASamplingContext,
    search_space: ARASearchSpace,
) -> ARATrajectoryParameters:
    """Sample the fixed eight-dimensional, non-truncated v2 search space."""
    if context.layer_count < 1:
        raise ValueError("layer_count must be positive")
    sampled = {
        name: _suggest_coordinate(trial, name, search_space)
        for name in ARA_PARAMETER_NAMES
    }
    start = min(
        context.layer_count - 1,
        int(sampled["layer_start"] * context.layer_count),
    )
    span = max(1, round(sampled["layer_span"] * context.layer_count))
    end = min(context.layer_count, start + span)
    return ARATrajectoryParameters(
        layer_start_fraction=sampled["layer_start"],
        layer_span_fraction=sampled["layer_span"],
        start_layer_index=start,
        end_layer_index=end,
        attn_strength=sampled["attn_strength"],
        mlp_strength=sampled["mlp_strength"],
        push_weight=sampled["push_weight"],
        margin=sampled["margin"],
        attn_deployment_gain=sampled["attn_deployment_gain"],
        mlp_deployment_gain=sampled["mlp_deployment_gain"],
    )


def parameter_envelope(parameters: ARATrajectoryParameters) -> dict[str, Any]:
    """Serialize resolved v2 parameters without a second Optuna conversion."""
    return {
        "method": "ara",
        "objective_version": "trajectory-v2",
        "search_space_version": SEARCH_SPACE_VERSION,
        "payload": asdict(parameters),
    }


def parse_parameter_envelope(
    envelope: Mapping[str, Any],
) -> ARATrajectoryParameters:
    """Strictly parse a trajectory-v2 parameter envelope."""
    if set(envelope) != {
        "method",
        "objective_version",
        "search_space_version",
        "payload",
    }:
        raise ValueError("trajectory parameter envelope fields are invalid")
    if envelope["method"] != "ara" or envelope["objective_version"] != "trajectory-v2":
        raise ValueError("trajectory parameter envelope version is invalid")
    if envelope["search_space_version"] != SEARCH_SPACE_VERSION:
        raise ValueError("trajectory search space version is unsupported")
    payload = envelope["payload"]
    if not isinstance(payload, Mapping):
        raise ValueError("trajectory parameter payload must be a mapping")
    expected = set(ARATrajectoryParameters.__dataclass_fields__)
    if set(payload) != expected:
        raise ValueError("trajectory parameter payload fields are invalid")
    indices = (payload["start_layer_index"], payload["end_layer_index"])
    if any(not isinstance(value, int) or isinstance(value, bool) for value in indices):
        raise ValueError("trajectory layer indices must be integers")
    if indices[0] < 0 or indices[1] <= indices[0]:
        raise ValueError("trajectory layer interval is invalid")
    coordinates = {
        "layer_start": payload["layer_start_fraction"],
        "layer_span": payload["layer_span_fraction"],
        **{name: payload[name] for name in ARA_PARAMETER_NAMES[2:]},
    }
    for name, value in coordinates.items():
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
        ):
            raise ValueError(f"trajectory coordinate {name} must be finite")
        numeric = float(value)
        invalid_fraction = (
            name == "layer_start"
            and not 0 <= numeric < 1
            or name == "layer_span"
            and not 0 < numeric <= 1
        )
        if invalid_fraction or (
            name not in {"layer_start", "layer_span"} and numeric <= 0
        ):
            raise ValueError(f"trajectory coordinate {name} is out of range")
    return ARATrajectoryParameters(**payload)


def _score_map(
    score_records: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    return {str(record["name"]): record for record in score_records}


def _finite_record_value(record: Mapping[str, Any], side: str) -> float:
    value = float(record[side]["value"])
    if not math.isfinite(value):
        raise ValueError(f"{side} score must be finite")
    return value


def calculate_candidate_constraints(
    score_records: Sequence[Mapping[str, Any]],
    gate: AcceptanceGate,
) -> tuple[float, float, float]:
    """Calculate the fixed Keywords/drop/KL candidate constraint tuple."""
    records = _score_map(score_records)
    try:
        keyword = records[gate.keyword_score]
        divergence = records[gate.kl_score]
    except KeyError as error:
        raise ValueError(f"required gate score is missing: {error.args[0]}") from error
    keyword_value = _finite_record_value(keyword, "score")
    keyword_baseline = _finite_record_value(keyword, "baseline")
    kl_value = _finite_record_value(divergence, "score")
    if keyword_baseline <= 0:
        raise ValueError("Keywords baseline must be positive")
    relative_drop = (keyword_baseline - keyword_value) / keyword_baseline
    constraints = (
        keyword_value - gate.keyword_max,
        gate.keyword_drop_min - relative_drop,
        kl_value - gate.kl_max,
    )
    if not all(math.isfinite(value) for value in constraints):
        raise ValueError("candidate constraints must be finite")
    return constraints


def set_candidate_constraints(
    trial: Trial,
    score_records: Sequence[Mapping[str, Any]],
    gate: AcceptanceGate,
) -> tuple[float, float, float]:
    """Persist candidate constraints before returning COMPLETE objectives."""
    constraints = calculate_candidate_constraints(score_records, gate)
    trial.set_user_attr(CANDIDATE_CONSTRAINTS_KEY, list(constraints))
    return constraints


def constraints_from_trial(trial: FrozenTrial) -> tuple[float, float, float]:
    """Read strict candidate constraints from a COMPLETE trial."""
    if trial.state != TrialState.COMPLETE:
        raise ValueError("constraints are defined only for COMPLETE trials")
    raw = trial.user_attrs.get(CANDIDATE_CONSTRAINTS_KEY)
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise ValueError("COMPLETE trial has no three candidate constraints")
    values = tuple(float(value) for value in raw)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("candidate constraints must be finite")
    return values[0], values[1], values[2]


def failure_record(
    trial_number: int,
    category: str,
    stage: str,
    error: BaseException,
    module_key: str | None = None,
) -> TrialFailureRecord:
    """Create a bounded, explicit failure record without message inference."""
    message = " ".join(str(error).split())[:500]
    return TrialFailureRecord(
        trial_number=trial_number,
        category=category,
        stage=stage,
        exception_type=type(error).__name__,
        message=message,
        is_runtime_failure=category in RUNTIME_FAILURE_CATEGORIES,
        module_key=module_key,
    )


def enqueue_anchor_prefix(study: Study, anchors: Sequence[ARASeedTrial]) -> None:
    """Validate the existing anchor prefix and enqueue only a missing suffix."""
    trials = sorted(study.get_trials(deepcopy=False), key=lambda trial: trial.number)
    if any(
        trial.number >= len(anchors) and trial.state == TrialState.WAITING
        for trial in trials
    ):
        raise RuntimeError("unexpected WAITING trial outside the anchor prefix")
    for trial in trials[: len(anchors)]:
        expected = anchors[trial.number].model_dump()
        actual = trial.user_attrs.get("anchor_parameters")
        if trial.number >= len(anchors) or actual != expected:
            raise RuntimeError("anchor prefix identity or ordering is invalid")
        if trial.params and trial.params != expected:
            raise RuntimeError("terminal anchor parameters differ from registration")
        if trial.user_attrs.get("anchor_index") != trial.number:
            raise RuntimeError("anchor prefix user attributes are invalid")
    for index in range(len(trials), len(anchors)):
        study.enqueue_trial(
            anchors[index].model_dump(),
            user_attrs={
                "protocol_phase": "anchor",
                "anchor_index": index,
                "anchor_parameters": anchors[index].model_dump(),
            },
            skip_if_exists=True,
        )


def recover_orphaned_trials(
    study: Study,
    exclusive: bool,
    recovery_path: str | Path | None = None,
) -> list[TrialFailureRecord]:
    """Fail orphan RUNNING attempts only when exclusive ownership is proven."""
    running = [
        trial
        for trial in study.get_trials(deepcopy=False)
        if trial.state == TrialState.RUNNING
    ]
    if running and not exclusive:
        raise RuntimeError("RUNNING trials require an exclusive recovery lock")
    records = []
    for trial in running:
        error = RuntimeError(f"orphaned attempt started at {trial.datetime_start}")
        record = failure_record(trial.number, "interrupted", "recovery", error)
        _append_recovery_record(recovery_path, record)
        recovery_records = list(study.user_attrs.get("recovery_records", []))
        recovery_records.append(asdict(record))
        study.set_user_attr("recovery_records", recovery_records)
        study.tell(trial.number, state=TrialState.FAIL)
        records.append(record)
    return records


def bind_study_identity(study: Study, fingerprint: str, artifacts: Any) -> None:
    """Bind one regenerated trajectory to a study before any new attempt."""
    study.set_user_attr("study_fingerprint", fingerprint)
    trajectory = getattr(artifacts, "trajectory_fingerprint", None)
    if trajectory is None:
        return
    previous = study.user_attrs.get("trajectory_fingerprint")
    if previous is not None and previous != trajectory:
        raise RuntimeError("study trajectory fingerprint changed on resume")
    for trial in study.trials:
        actual = trial.user_attrs.get("trajectory_fingerprint")
        if actual is not None and actual != trajectory:
            raise RuntimeError("trial trajectory fingerprint differs")
        if trial.state == TrialState.COMPLETE and actual is None:
            raise RuntimeError("completed trial trajectory fingerprint differs")
    study.set_user_attr("trajectory_fingerprint", trajectory)


def _append_recovery_record(
    recovery_path: str | Path | None,
    record: TrialFailureRecord,
) -> None:
    if recovery_path is None:
        return
    path = Path(recovery_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(asdict(record), allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def validate_trial_envelope(
    trials: Sequence[FrozenTrial],
    required_trials: int,
    require_terminal: bool = False,
) -> None:
    """Reject missing, duplicate, extra, or optionally non-terminal attempts."""
    numbers = [trial.number for trial in trials]
    if numbers != list(range(len(numbers))) or len(numbers) > required_trials:
        raise RuntimeError("study trial numbers are missing, duplicated, or extra")
    if require_terminal and (
        len(numbers) != required_trials
        or any(trial.state not in TERMINAL_STATES for trial in trials)
    ):
        raise RuntimeError("study does not contain the required terminal attempts")


def study_trials_and_recoveries(
    source: TrialSource,
) -> tuple[Trials, Sequence[Mapping[str, Any]]]:
    """Resolve trials and validate structured orphan-recovery evidence."""
    if not isinstance(source, Study):
        return source, ()
    recoveries = source.user_attrs.get("recovery_records", [])
    if not isinstance(recoveries, list) or any(
        not isinstance(item, Mapping) for item in recoveries
    ):
        raise ValueError("study recovery records are invalid")
    return source.trials, recoveries


def merged_failure_records(
    trials: Trials,
    recoveries: Sequence[Mapping[str, Any]],
) -> dict[int, Mapping[str, Any]]:
    """Merge trial and recovery failures by trial number without inference."""
    valid_numbers = {trial.number for trial in trials}
    records = {
        trial.number: record
        for trial in trials
        if isinstance((record := trial.user_attrs.get("failure_record")), Mapping)
    }
    for recovery in recoveries:
        number = recovery.get("trial_number")
        if not isinstance(number, int) or number not in valid_numbers:
            raise ValueError("recovery record trial number is invalid")
        previous = records.get(number)
        if previous is not None and dict(previous) != dict(recovery):
            raise ValueError("trial and recovery failure records conflict")
        records[number] = recovery
    return records


def _statistics(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {name: None for name in ("min", "p25", "median", "p75", "max")}
    finite = np.asarray(values, dtype=np.float64)
    if not np.isfinite(finite).all():
        raise ValueError("study summary values must be finite")
    quantiles = np.quantile(finite, [0, 0.25, 0.5, 0.75, 1])
    return dict(zip(("min", "p25", "median", "p75", "max"), quantiles.tolist()))


def _trial_score(trial: FrozenTrial, name: str) -> float | None:
    records = trial.user_attrs.get("score_records", [])
    for record in records:
        if record.get("name") == name:
            value = float(record["score"]["value"])
            return value if math.isfinite(value) else None
    return None


def _pareto_ids(trials: Sequence[FrozenTrial]) -> tuple[int, ...]:
    complete = [
        trial for trial in trials if trial.state == TrialState.COMPLETE and trial.values
    ]
    result = []
    for candidate in complete:
        dominated = any(
            other.number != candidate.number
            and all(a <= b for a, b in zip(other.values or (), candidate.values or ()))
            and any(a < b for a, b in zip(other.values or (), candidate.values or ()))
            for other in complete
        )
        if not dominated:
            result.append(candidate.number)
    return tuple(sorted(result))


def _score_statistics(
    trials: Trials,
    gate: AcceptanceGate,
) -> dict[str, dict[str, float | None]]:
    scores: dict[str, list[float]] = {
        gate.keyword_score: [],
        gate.kl_score: [],
    }
    for trial in trials:
        if trial.state != TrialState.COMPLETE:
            continue
        for record in trial.user_attrs.get("score_records", []):
            name = str(record.get("name"))
            value = _finite_record_value(record, "score")
            scores.setdefault(name, []).append(value)
    return {name: _statistics(values) for name, values in scores.items()}


def _constraint_statistics(
    trials: Trials,
) -> dict[str, dict[str, float | None]]:
    names = ("keyword_max", "keyword_drop_min", "kl_max")
    violations: dict[str, list[float]] = {name: [] for name in names}
    for trial in trials:
        if trial.state != TrialState.COMPLETE:
            continue
        try:
            values = constraints_from_trial(trial)
        except ValueError:
            continue
        for name, value in zip(names, values):
            violations[name].append(max(0.0, value))
    return {name: _statistics(values) for name, values in violations.items()}


def summarize_study(source: TrialSource, gate: AcceptanceGate) -> StudySummary:
    """Build finite diagnostics for acceptance and failed-study reports."""
    trials, recoveries = study_trials_and_recoveries(source)
    counts = {state.name: 0 for state in TrialState}
    categories: dict[str, int] = {}
    records = merged_failure_records(trials, recoveries)
    for trial in trials:
        counts[trial.state.name] += 1
        record = records.get(trial.number)
        if isinstance(record, Mapping) and record.get("is_runtime_failure") is True:
            category = str(record.get("category", "unknown"))
            categories[category] = categories.get(category, 0) + 1
    keyword_trials = [
        (value, trial.number)
        for trial in trials
        if trial.state == TrialState.COMPLETE
        and (value := _trial_score(trial, gate.keyword_score)) is not None
    ]
    objective_trials = [
        (trial.values[0], trial.number)
        for trial in trials
        if trial.state == TrialState.COMPLETE and trial.values
    ]
    boundary = _parameter_boundary_summary(trials, ARASearchSpace())
    failures = sum(categories.values())
    return StudySummary(
        state_counts=counts,
        runtime_failure_count=failures,
        runtime_failure_rate=failures / gate.required_trials,
        runtime_failure_categories=categories,
        score_statistics=_score_statistics(trials, gate),
        constraint_statistics=_constraint_statistics(trials),
        best_keyword_trial=min(keyword_trials)[1] if keyword_trials else None,
        best_objective_trial=min(objective_trials)[1] if objective_trials else None,
        pareto_trial_ids=_pareto_ids(trials),
        parameter_boundary_distances=boundary,
    )


def _parameter_boundary_summary(
    trials: Sequence[FrozenTrial],
    search_space: ARASearchSpace,
) -> dict[str, dict[str, float | None]]:
    result = {}
    complete = [trial for trial in trials if trial.state == TrialState.COMPLETE]
    for name in ARA_PARAMETER_NAMES:
        lower, upper = getattr(search_space, name)
        values = [
            float(trial.params[name]) for trial in complete if name in trial.params
        ]
        if not values:
            result[name] = {"lower": None, "upper": None}
            continue
        result[name] = {
            "lower": min((value - lower) / (upper - lower) for value in values),
            "upper": min((upper - value) / (upper - lower) for value in values),
        }
    return result
