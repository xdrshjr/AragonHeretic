# SPDX-License-Identifier: AGPL-3.0-or-later
"""固定 bank 和起点的整组回溯事务及逐条件拒绝证据。"""

from __future__ import annotations

from dataclasses import dataclass
import time

import torch

from .ara import get_lora_factors
from .ara_proposal import (
    ProposalPolicy,
    interpolate_update,
    project_proposal,
    relative_change,
)
from .ara_refinement_capture import (
    apply_snapshot,
    capture_fixed_sequences,
    snapshot_factors,
    tensor_identity,
    measure_research_phase,
)


@dataclass
class BacktrackingRequest:
    """求解完成后的固定事务输入，不允许回溯重新求解。"""

    model: object
    artifacts: object
    capture: object
    bank: object
    proposal: object
    parameters: object
    original: dict
    before: dict
    event: dict


def _pair(snapshot, name):
    return snapshot[f"{name}.A"], snapshot[f"{name}.B"]


def _policy(request, target):
    initial = getattr(request.artifacts, "initial_factors", request.original)
    return ProposalPolicy(
        request.artifacts.config.proposal_policy,
        zero_initialization=_pair(initial, target.full_name),
    )


def _project_group(request):
    results = {}
    for target in request.capture.targets:
        results[target.full_name] = project_proposal(
            _pair(request.proposal.factors, target.full_name),
            _policy(request, target),
        )
    return results


def _local_statistics(request, target, factors):
    from .ara_refinement import (
        SolverOptions,
        refinement_loss,
        weighted_output_ratio,
    )

    pair = request.bank.pairs[target.key]
    parameters = request.parameters
    strength = (
        parameters.mlp_strength
        if target.key.component == "mlp.down_proj"
        else parameters.attn_strength
    )
    options = SolverOptions(
        strength,
        parameters.push_weight,
        parameters.margin,
        request.artifacts.config.keep_reference,
    )
    a, b = factors
    terms = {
        key: float(value)
        for key, value in refinement_loss(pair, factors, options).items()
    }
    delta = (pair.good.inputs @ a.T) @ b.T
    terms["local_cumulative_ratio"] = weighted_output_ratio(
        pair.good.minus + delta,
        pair.good.reference,
        pair.good.weights,
    )
    terms["local_update_ratio"] = weighted_output_ratio(
        pair.good.reference + delta,
        pair.good.reference,
        pair.good.weights,
    )
    return terms


def _candidate(request, projected, alpha):
    factors, statistics = {}, {}
    for target in request.capture.targets:
        name = target.full_name
        previous = _pair(request.original, name)
        result = interpolate_update(
            previous,
            projected[name].factors,
            alpha,
            _policy(request, target),
        )
        a, b = result.factors
        factors[f"{name}.A"], factors[f"{name}.B"] = a, b
        statistics[name] = {
            **result.statistics,
            **_local_statistics(request, target, result.factors),
        }
    return factors, statistics


def _actual_statistics(request):
    from .ara_refinement import weighted_output_ratio

    actual = capture_fixed_sequences(
        request.model,
        request.capture.targets,
        request.capture.sequences["good"],
    )
    result, discrepancy = {}, {}
    for target in request.capture.targets:
        observation = actual[target.key]
        pair = request.bank.pairs[target.key].good
        lookup = {
            (int(p), int(s)): i
            for i, (p, s) in enumerate(
                zip(
                    observation.prompt_indices,
                    observation.step_indices,
                    strict=True,
                )
            )
        }
        rows = [
            lookup[(int(p), int(s))]
            for p, s in zip(pair.prompt_indices, pair.step_indices, strict=True)
        ]
        outputs = observation.outputs[rows]
        a, b = (value.detach().cpu() for value in get_lora_factors(target))
        predicted = pair.minus + (pair.inputs @ a.T) @ b.T
        result[target.full_name] = weighted_output_ratio(
            outputs,
            pair.reference,
            pair.weights,
        )
        # 使用 y_ref 的分母，不能将 predicted 的幅度当作归一化基准。
        discrepancy[target.full_name] = weighted_output_ratio(
            pair.reference + outputs - predicted,
            pair.reference,
            pair.weights,
        )
    return result, discrepancy


def _monitor_guards(before, after):
    from .ara_refinement import monitor_accepts

    accepted = monitor_accepts(before, after)
    guards = {
        "monitor_keywords": after["keywords"] <= before["keywords"],
        "monitor_log_odds": after["log_odds"] <= before["log_odds"] + 1e-4,
        "monitor_first_kl": 0 <= after["first_token_kl"] <= 0.15,
        "monitor_sequence_kl": 0 <= after["sequence_kl"] <= 0.15,
        "no_strict_improvement": (
            before["keywords"] - after["keywords"]
            >= 1 / before["sample_count"] - 1e-12
            or before["log_odds"] - after["log_odds"] >= 1e-4
        ),
    }
    return accepted, guards


def _evaluate_attempt(request, factors, statistics, row):
    guards = row["guards"]
    guards["projected_spectral_guard"] = all(
        value["projected_spectral_norm"] <= 8 for value in statistics.values()
    )
    guards["local_cumulative_guard"] = all(
        value["local_cumulative_ratio"] <= 0.60 for value in statistics.values()
    )
    guards["no_effective_change"] = any(
        relative_change(
            _pair(request.original, target.full_name),
            _pair(factors, target.full_name),
        )
        > 1e-6
        for target in request.capture.targets
    )
    if not all(value for value in guards.values() if value is not None):
        return False
    apply_snapshot(request.capture.targets, factors)
    with measure_research_phase(
        request.model, "actual_forward", row["phase_resources"]
    ):
        actual, discrepancy = _actual_statistics(request)
    row.update(
        actual_cumulative_ratios=actual, prediction_discrepancy=discrepancy
    )
    if not all(
        torch.isfinite(torch.tensor(value))
        for value in (*actual.values(), *discrepancy.values())
    ):
        raise RuntimeError("真实联合输出统计非有限")
    guards["actual_cumulative_guard"] = all(x <= 0.60 for x in actual.values())
    if not guards["actual_cumulative_guard"]:
        return False
    request.artifacts.check_budget()
    with measure_research_phase(
        request.model, "monitor_after", row["phase_resources"]
    ):
        row["monitor_after"] = request.artifacts.monitor(request.model)
    accepted, monitor = _monitor_guards(request.before, row["monitor_after"])
    guards.update(monitor)
    return accepted


def _attempt(request, projected, index, alpha):
    request.artifacts.check_budget()
    started = time.monotonic()
    factors, statistics = _candidate(request, projected, alpha)
    row = {
        "index": index,
        "alpha": alpha,
        "accepted": False,
        "phase_resources": [],
        "before_adapter_hash": request.event["before_adapter_hash"],
        "candidate_hash": tensor_identity(factors),
        "statistics": statistics,
        "monitor_after": None,
        "actual_cumulative_ratios": None,
        "prediction_discrepancy": None,
        "guards": {
            name: None
            for name in (
                "projected_spectral_guard",
                "local_cumulative_guard",
                "no_effective_change",
                "actual_cumulative_guard",
                "monitor_keywords",
                "monitor_log_odds",
                "monitor_first_kl",
                "monitor_sequence_kl",
                "no_strict_improvement",
            )
        },
    }
    try:
        row["accepted"] = _evaluate_attempt(request, factors, statistics, row)
    finally:
        if not row["accepted"]:
            apply_snapshot(request.capture.targets, request.original)
        row["restored_hash"] = tensor_identity(
            snapshot_factors(request.model.ara_targets)
        )
        row["wallclock_seconds"] = time.monotonic() - started
    row["reasons"] = [
        name for name, passed in row["guards"].items() if passed is False
    ]
    return row


def evaluate_block_proposals(context, request):
    """整组同步尝试固定 alpha，异常和每次拒绝均恢复原始因子。"""
    event = request.event
    event.update(
        proposal_policy=context.config.proposal_policy,
        selected_alpha=None,
        backtracking_attempts=[],
    )
    projected = _project_group(request)
    event["projected_statistics"] = {
        name: value.statistics for name, value in projected.items()
    }
    try:
        for index, alpha in enumerate(context.config.backtracking_alphas):
            row = _attempt(request, projected, index, alpha)
            event["backtracking_attempts"].append(row)
            if row["accepted"]:
                event.update(
                    accepted=True,
                    selected_alpha=alpha,
                    rejection_reason=None,
                    monitor_after=row["monitor_after"],
                    prediction_discrepancy=row["prediction_discrepancy"],
                    actual_cumulative_ratios=row["actual_cumulative_ratios"],
                )
                break
        if not event["accepted"]:
            event["rejection_reason"] = "backtracking_exhausted"
    finally:
        if not event["accepted"]:
            apply_snapshot(request.capture.targets, request.original)
    event["after_adapter_hash"] = tensor_identity(
        snapshot_factors(request.model.ara_targets)
    )
    return event
