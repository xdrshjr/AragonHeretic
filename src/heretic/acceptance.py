# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure trajectory-v2 acceptance, health gates, and replay checks."""

from __future__ import annotations

import math
from pathlib import Path
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Sequence

import torch
from optuna.trial import FrozenTrial, TrialState
from torch import Tensor

from .ara_config import AcceptanceGate
from .ara import ARAOptimizationError
from .ara_search import (
    StudySummary,
    TrialSource,
    constraints_from_trial,
    merged_failure_records,
    study_trials_and_recoveries,
    summarize_study,
    validate_trial_envelope,
)

LegacyRecords = Sequence[Mapping[str, Any]]


class AcceptanceGateError(RuntimeError):
    """Raised when study, validation, replay, audit, or export gates fail."""


def legacy_trial_failure_record(error: BaseException, stage: str) -> dict[str, Any]:
    """Create the point-v1 journal-safe failure record."""
    if isinstance(error, ARAOptimizationError):
        return error.to_record()
    return {"type": type(error).__name__, "stage": stage, "message": str(error)[:500]}


def legacy_failure_constraint(trial: FrozenTrial) -> tuple[float]:
    """Keep point-v1 value-less failures out of its TPE model."""
    return (1.0 if "failure" in trial.user_attrs else 0.0,)


def safe_failure_reason(error: BaseException) -> str:
    """Remove local absolute roots before persisting an error reason."""
    message = str(error)[:1000]
    for label, root in (("<workspace>", Path.cwd()), ("<home>", Path.home())):
        for value in {str(root), root.as_posix()}:
            message = message.replace(value, label)
    return message


@dataclass(frozen=True)
class GateValues:
    """Validated scalar values used for deterministic candidate selection."""

    keyword: float
    keyword_baseline: float
    relative_keyword_drop: float
    kl: float


@dataclass(frozen=True)
class ReplayResult:
    """Scores and adapter state from one deterministic replay."""

    score_records: tuple[Mapping[str, Any], ...]
    adapter_state: Mapping[str, Tensor]


@dataclass(frozen=True)
class ReplayEvidence:
    """Two replay results plus maximum adapter and KL drift."""

    first: ReplayResult
    second: ReplayResult
    max_absolute_error: float
    max_relative_error: float
    kl_drift: float


@dataclass(frozen=True)
class ReplayContext:
    """Callbacks needed to apply, score, capture, and clean one candidate."""

    apply: Callable[[FrozenTrial], None]
    score: Callable[[], Sequence[Mapping[str, Any]]]
    capture_state: Callable[[], Mapping[str, Tensor]]
    cleanup: Callable[[], None]
    gate: AcceptanceGate


def _records_by_name(
    score_records: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    records = {}
    for record in score_records:
        name = record.get("name")
        if not isinstance(name, str) or name in records:
            raise AcceptanceGateError("score names must be present and unique")
        records[name] = record
    return records


def _evidence(record: Mapping[str, Any], expected: int) -> tuple[float, float]:
    sides = []
    fingerprints = []
    for side in ("score", "baseline"):
        item = record.get(side)
        if not isinstance(item, Mapping):
            raise AcceptanceGateError(f"score has no {side} evidence")
        value = item.get("value")
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise AcceptanceGateError(f"score {side} value is not finite")
        if item.get("sample_count") != expected:
            raise AcceptanceGateError(
                f"score {side} sample count does not equal {expected}"
            )
        fingerprint = item.get("dataset_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise AcceptanceGateError(f"score {side} has no dataset fingerprint")
        sides.append(float(value))
        fingerprints.append(fingerprint)
    if fingerprints[0] != fingerprints[1]:
        raise AcceptanceGateError("score and baseline dataset identities differ")
    return sides[0], sides[1]


def validate_gate_records(
    score_records: Sequence[Mapping[str, Any]],
    gate: AcceptanceGate,
) -> GateValues:
    """Validate machine evidence and calculate the relative keyword reduction."""
    records = _records_by_name(score_records)
    try:
        keyword_record = records[gate.keyword_score]
        kl_record = records[gate.kl_score]
    except KeyError as error:
        raise AcceptanceGateError(f"missing gate score: {error.args[0]}") from error
    keyword, keyword_baseline = _evidence(
        keyword_record,
        gate.expected_samples,
    )
    kl, _ = _evidence(kl_record, gate.expected_samples)
    if not 0 <= keyword <= 1 or not 0 < keyword_baseline <= 1:
        raise AcceptanceGateError("Keywords values are outside their valid range")
    if kl < 0:
        raise AcceptanceGateError("KL divergence must not be negative")
    relative_drop = (keyword_baseline - keyword) / keyword_baseline
    return GateValues(keyword, keyword_baseline, relative_drop, kl)


def validate_study_health(
    source: TrialSource,
    gate: AcceptanceGate,
) -> None:
    """Require the complete fixed attempt envelope and runtime health limits."""
    try:
        trials, recoveries = study_trials_and_recoveries(source)
    except ValueError as error:
        raise AcceptanceGateError(str(error)) from error
    ordered = sorted(trials, key=lambda trial: trial.number)
    try:
        validate_trial_envelope(ordered, gate.required_trials, require_terminal=True)
    except RuntimeError as error:
        raise AcceptanceGateError(str(error)) from error
    complete = sum(trial.state == TrialState.COMPLETE for trial in ordered)
    if complete < gate.min_complete_trials:
        raise AcceptanceGateError(
            f"only {complete} trials completed; {gate.min_complete_trials} required"
        )
    for trial in ordered:
        if trial.state == TrialState.COMPLETE:
            try:
                constraints_from_trial(trial)
            except ValueError as error:
                raise AcceptanceGateError(
                    f"trial {trial.number} has invalid candidate constraints"
                ) from error
    try:
        failures = merged_failure_records(ordered, recoveries)
    except ValueError as error:
        raise AcceptanceGateError(str(error)) from error
    failure_rate = (
        sum(record.get("is_runtime_failure") is True for record in failures.values())
        / gate.required_trials
    )
    if failure_rate > gate.max_runtime_failure_rate:
        raise AcceptanceGateError(
            f"runtime failure rate {failure_rate:.3f} exceeds the gate"
        )


def select_for_acceptance(
    source: TrialSource,
    gate: AcceptanceGate,
    study_fingerprint: str,
) -> FrozenTrial:
    """Select the unique lexicographic trajectory-v2 validation candidate."""
    validate_study_health(source, gate)
    trials, _ = study_trials_and_recoveries(source)
    accepted = []
    for trial in trials:
        if trial.state != TrialState.COMPLETE:
            continue
        if trial.user_attrs.get("study_fingerprint") != study_fingerprint:
            continue
        if trial.user_attrs.get("objective_version") != "trajectory-v2":
            continue
        records = trial.user_attrs.get("score_records", trial.user_attrs.get("scores"))
        if not isinstance(records, list):
            continue
        try:
            values = validate_gate_records(records, gate)
        except AcceptanceGateError:
            continue
        if (
            values.keyword <= gate.keyword_max
            and values.relative_keyword_drop >= gate.keyword_drop_min
            and values.kl <= gate.kl_max
        ):
            accepted.append(((values.keyword, values.kl, trial.number), trial))
    if not accepted:
        raise AcceptanceGateError("no trial passed the acceptance gate")
    accepted.sort(key=lambda item: item[0])
    return accepted[0][1]


def _state_errors(
    left: Mapping[str, Tensor],
    right: Mapping[str, Tensor],
) -> tuple[float, float]:
    if left.keys() != right.keys():
        raise AcceptanceGateError("replay adapter state keys differ")
    maximum_absolute = 0.0
    maximum_relative = 0.0
    for name in left:
        first = left[name].to(torch.float32)
        second = right[name].to(torch.float32)
        if not torch.allclose(first, second, rtol=1e-6, atol=1e-7):
            raise AcceptanceGateError(f"replay adapter tensor differs: {name}")
        difference = (first - second).abs()
        maximum_absolute = max(maximum_absolute, float(difference.max()))
        denominator = first.abs().clamp_min(1e-7)
        maximum_relative = max(
            maximum_relative,
            float((difference / denominator).max()),
        )
    return maximum_absolute, maximum_relative


def _one_replay(context: ReplayContext, trial: FrozenTrial) -> ReplayResult:
    context.apply(trial)
    try:
        records = tuple(context.score())
        validate_gate_records(records, context.gate)
        state = {
            name: tensor.detach().cpu().clone()
            for name, tensor in context.capture_state().items()
        }
        return ReplayResult(records, state)
    finally:
        context.cleanup()


def replay_candidate(
    context: ReplayContext,
    trial: FrozenTrial,
) -> ReplayEvidence:
    """Replay a locked candidate twice and require deterministic state/scores."""
    first = _one_replay(context, trial)
    second = _one_replay(context, trial)
    absolute, relative = _state_errors(first.adapter_state, second.adapter_state)
    first_values = validate_gate_records(first.score_records, context.gate)
    second_values = validate_gate_records(second.score_records, context.gate)
    if first_values.keyword != second_values.keyword:
        raise AcceptanceGateError("replay Keywords values differ")
    kl_drift = abs(first_values.kl - second_values.kl)
    if kl_drift > 0.005:
        raise AcceptanceGateError("replay KL drift exceeds 0.005")
    return ReplayEvidence(first, second, absolute, relative, kl_drift)


def failed_acceptance_payload(
    source: TrialSource,
    gate: AcceptanceGate,
    study_fingerprint: str,
    stage: str,
    audit_consumed: bool = False,
) -> dict[str, Any]:
    """Build a schema-v2 diagnostic report without claiming acceptance."""
    trials, recoveries = study_trials_and_recoveries(source)
    summary: StudySummary = summarize_study(source, gate)
    failures = merged_failure_records(trials, recoveries)
    return {
        "schema": "cara-acceptance-v2",
        "status": "failed",
        "failure_stage": stage,
        "study_fingerprint": study_fingerprint,
        "selected_trial_number": None,
        "audit_consumed": audit_consumed,
        "study_summary": asdict(summary),
        "failure_samples": [failures[number] for number in sorted(failures)[:20]],
    }


def validate_legacy_gate_records(
    score_records: LegacyRecords, gate: AcceptanceGate
) -> tuple[float, float]:
    """Validate point-v1 score records while preserving display-count support."""
    records = _records_by_name(score_records)
    try:
        keyword = records[gate.keyword_score]
        divergence = records[gate.kl_score]
    except KeyError as error:
        raise ValueError(f"missing gate score: {error.args[0]}") from error

    def finite(record: Mapping[str, Any], field: str) -> float:
        container = record.get(field)
        if not isinstance(container, Mapping):
            raise ValueError(f"score has no {field} object")
        value = container.get("value")
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"score {field} value is not finite")
        return float(value)

    score = keyword.get("score", {})
    sample_count = score.get("sample_count")
    if sample_count is None:
        display = str(score.get("md_display", score.get("rich_display", "")))
        try:
            sample_count = int(display.rsplit("/", 1)[1])
        except (IndexError, ValueError) as error:
            raise ValueError("keyword score does not expose a sample count") from error
    if sample_count != gate.expected_samples:
        raise ValueError(
            f"keyword score used {sample_count} samples, "
            f"expected {gate.expected_samples}"
        )
    keyword_value = finite(keyword, "score")
    keyword_baseline = finite(keyword, "baseline")
    divergence_value = finite(divergence, "score")
    divergence_baseline = finite(divergence, "baseline")
    values = (
        keyword_value,
        keyword_baseline,
        divergence_value,
        divergence_baseline,
    )
    if any(value < 0 or value > 1 for value in values):
        raise ValueError("gate scores must be in [0, 1]")
    if keyword_value > gate.keyword_max or divergence_value > gate.kl_max:
        raise ValueError("trial exceeds an absolute gate threshold")
    if keyword_baseline - keyword_value < gate.keyword_drop_min:
        raise ValueError("trial does not meet the relative keyword reduction")
    return keyword_value, divergence_value


def select_legacy_candidate(
    trials: Sequence[FrozenTrial],
    gate: AcceptanceGate,
    study_fingerprint: str,
) -> FrozenTrial:
    """Select a point-v1 candidate without trajectory study-health semantics."""
    accepted = []
    invalid_reasons = []
    for trial in trials:
        if trial.state != TrialState.COMPLETE:
            continue
        if trial.user_attrs.get("method") != "ara":
            continue
        if trial.user_attrs.get("study_fingerprint") != study_fingerprint:
            continue
        records = trial.user_attrs.get("scores")
        if not isinstance(records, list):
            invalid_reasons.append(f"trial {trial.number}: no score records")
            continue
        try:
            keyword, divergence = validate_legacy_gate_records(records, gate)
        except (ValueError, AcceptanceGateError) as error:
            invalid_reasons.append(f"trial {trial.number}: {error}")
            continue
        accepted.append(((keyword, divergence, trial.number), trial))
    if not accepted:
        details = "; ".join(invalid_reasons[:5])
        raise ValueError(
            "no trial passed the acceptance gate" + (f": {details}" if details else "")
        )
    accepted.sort(key=lambda item: item[0])
    return accepted[0][1]


def collect_legacy_failure_records(
    trials: Sequence[FrozenTrial],
) -> list[dict[str, Any]]:
    """Collect point-v1 structured failure attributes."""
    return [
        {"trial_number": trial.number, **trial.user_attrs["failure"]}
        for trial in trials
        if isinstance(trial.user_attrs.get("failure"), dict)
    ]
