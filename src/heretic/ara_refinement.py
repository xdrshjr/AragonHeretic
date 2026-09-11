# SPDX-License-Identifier: AGPL-3.0-or-later
"""基座锚定局部求解、绝对因子替换和层组接受事务。"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import torch
from torch import Tensor

from .ara import (
    BALANCE_WEIGHT,
    _canonical_factors,
    get_lora_factors,
    snapshot_adapter_state,
)
from .ara_refinement_capture import (
    CaptureRequest,
    PairedObservation,
    ReferencePair,
    apply_snapshot,
    bind_evaluation_state,
    capture_fixed_sequences,
    capture_reference_pair,
    disabled_targets,
    generate_sequences,
    snapshot_factors,
    tensor_identity,
)
from .ara_research_schema import digest, file_digest, write_json
from .ara_trajectory import _soft_nearest


class RefinementNumericalError(RuntimeError):
    """实现或数值异常：整个 attempt 失败，不能伪装成普通拒绝。"""


@dataclass(frozen=True)
class BlockProposal:
    """完整克隆因子及每模块部署统计；构造时不修改模型。"""

    factors: dict[str, Tensor]
    statistics: dict
    is_acceptable: bool
    rejection_reason: str | None


@dataclass(frozen=True)
class SolverOptions:
    """固定优化及部署契约。"""

    strength: float
    push_weight: float
    margin: float
    keep_reference: str = "base-anchored"
    max_iter: int = 20


@dataclass
class RefinementArtifacts:
    """显式注入不同数据角色的评分回调，避免隐式审计访问。"""

    protocol: dict
    config: object
    fit_prompts: dict
    fit_ids: dict
    monitor: Callable
    development: Callable
    output_dir: Path
    seed: int
    attempt: int
    check_budget: Callable = lambda: None


def _device_pair(pair, device):
    def transfer(observation):
        return PairedObservation(
            **{
                name: getattr(observation, name).to(device)
                for name in PairedObservation.__dataclass_fields__
            }
        )

    return ReferencePair(transfer(pair.good), transfer(pair.bad), pair.scales)


def _step_terms(pair, factors, step, options):
    a, b = factors
    good = pair.good.step_indices == step
    bad = pair.bad.step_indices == step
    reference_good = pair.good.reference[good]
    reference_bad = pair.bad.reference[bad]
    delta_good = (pair.good.inputs[good] @ a.T) @ b.T
    delta_bad = (pair.bad.inputs[bad] @ a.T) @ b.T
    z_good = pair.good.minus[good] + delta_good
    z_bad = pair.bad.minus[bad] + delta_bad
    residual = z_good - reference_good
    if options.keep_reference == "local-update":
        residual = delta_good
    denominator = reference_good.shape[1] * pair.scales[step]
    keep = residual.square().sum(dim=1) / denominator
    pull = _soft_nearest(
        torch.cdist(z_bad, reference_good).square() / denominator, 0.10
    )
    near_bad = _soft_nearest(
        torch.cdist(z_bad, reference_bad).square() / denominator, 0.10
    )
    push = 0.10 * torch.nn.functional.softplus(
        (options.margin - near_bad) / 0.10
    )
    return (
        (keep * pair.good.weights[good]).sum(),
        (pull * pair.bad.weights[bad]).sum(),
        (push * pair.bad.weights[bad]).sum(),
    )


def refinement_loss(pair, factors, options: SolverOptions) -> dict[str, Tensor]:
    """固定 bank 下计算 prompt/step 加权的全基座锚定目标。"""
    totals = [_step_terms(pair, factors, step, options) for step in pair.scales]
    keep = (
        torch.stack([row[0] for row in totals]).sum() / pair.good.weights.sum()
    )
    pull = (
        torch.stack([row[1] for row in totals]).sum() / pair.bad.weights.sum()
    )
    push = (
        torch.stack([row[2] for row in totals]).sum() / pair.bad.weights.sum()
    )
    a, b = factors
    balance = (a @ a.T - b.T @ b).square().mean()
    total = keep + options.strength * (pull + options.push_weight * push)
    total = total + BALANCE_WEIGHT * balance
    return {
        "keep": keep,
        "pull": pull,
        "push": push,
        "balance": balance,
        "total": total,
    }


def weighted_output_ratio(actual, reference, weights) -> float:
    """逐题逐步 RMS 比例后加权，不用组平均掩盖模块越界。"""
    numerator = (actual - reference).square().mean(dim=1).sqrt()
    denominator = reference.square().mean(dim=1).sqrt().clamp_min(1e-6)
    return float((numerator / denominator * weights).sum() / weights.sum())


def _check_effective_update(original, canonical):
    old_a, old_b = original
    new_a, new_b = canonical
    # 检查部署因子的数学更新，避免 FP32 累加舍入制造不等价。
    old_a, new_a = old_a.detach().double(), new_a.detach().double()
    for start in range(0, old_b.shape[0], 128):
        before = old_b[start : start + 128].detach().double() @ old_a
        after = new_b[start : start + 128].detach().double() @ new_a
        if not torch.allclose(before, after, rtol=1e-6, atol=1e-7):
            raise RefinementNumericalError("canonicalization 改变有效更新")


def _optimize_module(pair, factors, options):
    a, b = (factor.detach().clone().requires_grad_(True) for factor in factors)
    pair = _device_pair(pair, a.device)
    optimizer = torch.optim.LBFGS(
        [a, b],
        max_iter=options.max_iter,
        history_size=10,
        line_search_fn="strong_wolfe",
    )

    def closure():
        optimizer.zero_grad(set_to_none=True)
        loss = refinement_loss(pair, (a, b), options)["total"]
        if not torch.isfinite(loss):
            raise RefinementNumericalError("局部 loss 非有限")
        loss.backward()
        if not all(torch.isfinite(factor.grad).all() for factor in (a, b)):
            raise RefinementNumericalError("局部梯度非有限")
        return loss

    optimizer.step(closure)
    with torch.no_grad():
        ca, cb, singular = _canonical_factors(a, b)
        _check_effective_update((a, b), (ca, cb))
        terms = refinement_loss(pair, (ca, cb), options)
        delta = (pair.good.inputs @ ca.T) @ cb.T
        statistics = {key: float(value) for key, value in terms.items()}
        statistics.update(
            local_cumulative_ratio=weighted_output_ratio(
                pair.good.minus + delta, pair.good.reference, pair.good.weights
            ),
            local_update_ratio=weighted_output_ratio(
                pair.good.reference + delta,
                pair.good.reference,
                pair.good.weights,
            ),
            max_singular_value=float(singular.max()),
        )
    if not all(math.isfinite(value) for value in statistics.values()):
        raise RefinementNumericalError("部署统计非有限")
    return ca.detach().cpu(), cb.detach().cpu(), statistics


def solve_refinement_block(model, bank, proposal) -> BlockProposal:
    """克隆求解全组绝对 A/B；有限但越界的候选正常拒绝。"""
    targets, parameters, keep_reference = proposal
    factors, statistics = {}, {}
    for target in targets:
        strength = (
            parameters.mlp_strength
            if target.key.component == "mlp.down_proj"
            else parameters.attn_strength
        )
        options = SolverOptions(
            strength, parameters.push_weight, parameters.margin, keep_reference
        )
        a, b, stats = _optimize_module(
            bank.pairs[target.key],
            get_lora_factors(target),
            options,
        )
        factors[f"{target.full_name}.A"] = a
        factors[f"{target.full_name}.B"] = b
        statistics[target.full_name] = stats
    is_acceptable = all(
        row["local_cumulative_ratio"] <= 0.60
        and row["max_singular_value"] <= 8.0
        for row in statistics.values()
    )
    return BlockProposal(
        factors,
        statistics,
        is_acceptable,
        None if is_acceptable else "proposal_rejected",
    )


def monitor_accepts(before: dict, after: dict) -> bool:
    """Keywords/log-odds 必须非增且至少一项严格改善。"""
    names = ("keywords", "log_odds", "first_token_kl", "sequence_kl")
    for scores in (before, after):
        if not all(math.isfinite(scores[name]) for name in names):
            raise RefinementNumericalError("monitor 指标非有限")
        if not 0 <= scores["keywords"] <= 1:
            raise ValueError("monitor Keywords 不在 [0,1]")
    if (
        before["sample_count"] != after["sample_count"]
        or before["sample_count"] <= 0
    ):
        raise ValueError("monitor 前后分母不匹配")
    if before["data_identity"] != after["data_identity"]:
        raise ValueError("monitor 前后数据身份不匹配")
    if not 0 <= after["first_token_kl"] <= 0.15:
        return False
    if not 0 <= after["sequence_kl"] <= 0.15:
        return False
    non_increasing = (
        after["keywords"] <= before["keywords"]
        and after["log_odds"] <= before["log_odds"] + 1e-4
    )
    improved = (
        before["keywords"] - after["keywords"]
        >= 1 / before["sample_count"] - 1e-12
    )
    improved = improved or before["log_odds"] - after["log_odds"] >= 1e-4
    return non_increasing and improved


def _actual_ratios(model, targets, sequences, bank):
    actual = capture_fixed_sequences(model, targets, sequences["good"])
    ratios = {}
    for target in targets:
        observation = actual[target.key]
        pair = bank.pairs[target.key].good
        lookup = {
            (int(prompt), int(step)): index
            for index, (prompt, step) in enumerate(
                zip(
                    observation.prompt_indices,
                    observation.step_indices,
                    strict=True,
                )
            )
        }
        rows = [
            lookup[(int(prompt), int(step))]
            for prompt, step in zip(
                pair.prompt_indices, pair.step_indices, strict=True
            )
        ]
        ratios[target.full_name] = weighted_output_ratio(
            observation.outputs[rows],
            pair.reference,
            pair.weights,
        )
    if not all(math.isfinite(value) for value in ratios.values()):
        raise RefinementNumericalError("真实联合输出偏移非有限")
    return ratios


def _block_transaction(model, context, request, parameters, frozen=None):
    targets = request.targets
    before_state = tensor_identity(snapshot_factors(model.ara_targets))
    before = context.monitor(model)
    original = snapshot_factors(targets)
    bank = frozen or capture_reference_pair(model, request)
    bank = bind_evaluation_state(bank, before_state)
    proposal = solve_refinement_block(
        model,
        bank,
        (targets, parameters, context.config.keep_reference),
    )
    event = {
        "before_adapter_hash": before_state,
        "accepted": False,
        "bank_manifest": bank.manifest,
        "capture_manifest_hash": bank.capture_manifest_hash,
        "tensor_content_hash": bank.tensor_content_hash,
        "local_statistics": proposal.statistics,
        "monitor_before": before,
        "rejection_reason": proposal.rejection_reason,
    }
    try:
        if proposal.is_acceptable:
            apply_snapshot(targets, proposal.factors)
            actual = _actual_ratios(model, targets, request.sequences, bank)
            event["actual_cumulative_ratios"] = actual
            after = context.monitor(model)
            event["monitor_after"] = after
            event["accepted"] = all(
                value <= 0.60 for value in actual.values()
            ) and monitor_accepts(before, after)
            event["rejection_reason"] = (
                None if event["accepted"] else "monitor_guard"
            )
    finally:
        if not event["accepted"]:
            apply_snapshot(targets, original)
    event["after_adapter_hash"] = tensor_identity(
        snapshot_factors(model.ara_targets)
    )
    return event


def _layer_blocks(targets, parameters, block_size):
    count = max(target.key.layer_index for target in targets) + 1
    start = math.floor(parameters.layer_start * count)
    stop = min(count, start + math.ceil(parameters.layer_span * count))
    grouped = {}
    for target in sorted(targets, key=lambda target: target.key):
        if start <= target.key.layer_index < stop:
            grouped.setdefault(target.key.layer_index // block_size, []).append(
                target
            )
    return [tuple(grouped[key]) for key in sorted(grouped)]


def _fit_sequences(model, context):
    return {
        side: generate_sequences(
            model, context.fit_prompts[side], context.fit_ids[side], 8
        )
        for side in ("good", "bad")
    }


def _run_sweeps(model, context, parameters):
    blocks = _layer_blocks(
        model.ara_targets, parameters, context.config.block_layers
    )
    with disabled_targets(model.ara_targets):
        sequences = _fit_sequences(model, context)
    events, frozen = [], {}
    if context.config.capture_mode == "frozen-diagnostic":
        for index, targets in enumerate(blocks):
            frozen[index] = capture_reference_pair(
                model, _request(context, targets, sequences)
            )
    for sweep in range(context.config.sweeps):
        if sweep and context.config.trajectory_policy == "refresh-each-sweep":
            sequences = _fit_sequences(model, context)
        accepted = 0
        for index, targets in enumerate(blocks):
            resources_before = context.check_budget()
            print(
                f"ARA v3 sweep={sweep} block={index}/{len(blocks)} "
                f"modules={len(targets)}: 开始求解",
                flush=True,
            )
            request = _request(context, targets, sequences)
            event = _block_transaction(
                model, context, request, parameters, frozen.get(index)
            )
            event.update(attempt=context.attempt, sweep=sweep, block_id=index)
            event["resources"] = {
                "before": resources_before,
                "after": context.check_budget(),
            }
            events.append(event)
            print(
                f"ARA v3 sweep={sweep} block={index}: "
                f"accepted={event['accepted']} "
                f"reason={event['rejection_reason']}",
                flush=True,
            )
            accepted += int(event["accepted"])
            context.check_budget()
        if not accepted:
            break
    return events


def _request(context, targets, sequences):
    return CaptureRequest(
        targets,
        sequences,
        context.protocol["protocol_hash"],
        digest(context.protocol["model_identity"]),
        context.config.capture_mode,
    )


def apply_refinement_trial(model, artifacts, parameters) -> dict:
    """trial 边界初始化，保存与评分对应的独立快照后恢复全体因子。"""
    original = snapshot_factors(model.ara_targets)
    started = time.time()
    common_seed = int(
        digest(
            [
                artifacts.protocol["protocol_hash"],
                artifacts.seed,
                artifacts.attempt,
            ]
        )[:15],
        16,
    )
    try:
        snapshot_adapter_state(model.ara_targets, common_seed)
        if artifacts.config.method_id in {"B1", "B2"}:
            events = apply_reference_trial(model, artifacts, parameters)
        else:
            events = _run_sweeps(model, artifacts, parameters)
        scores = artifacts.development(model)
        snapshot = snapshot_factors(model.ara_targets)
        snapshot_hash = tensor_identity(snapshot)
        artifacts.output_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = artifacts.output_dir / "factors.pt"
        torch.save(snapshot, snapshot_path)
        evidence = {
            "protocol_hash": artifacts.protocol["protocol_hash"],
            "parameter_identity": digest(parameters.envelope()),
            "attempt": artifacts.attempt,
            "state": "COMPLETE",
            "failure_category": None,
            "events": events,
            "scores": scores,
            "final_snapshot_path": snapshot_path.resolve().as_posix(),
            "final_snapshot_hash": snapshot_hash,
            "snapshot_file_hash": file_digest(snapshot_path),
            "started_at": started,
            "stopped_at": time.time(),
            "wallclock_seconds": time.time() - started,
            "resources": artifacts.check_budget(),
        }
        write_json(
            artifacts.output_dir / "trial.json", evidence, immutable=True
        )
        return evidence
    finally:
        apply_snapshot(model.ara_targets, original)


def _capture_legacy_reference(model, artifacts):
    from .ara import build_calibration_bank
    from .ara_trajectory import build_trajectory_bank

    is_point = artifacts.config.method_id == "B1"
    sequences = {
        side: generate_sequences(
            model,
            artifacts.fit_prompts[side],
            artifacts.fit_ids[side],
            1 if is_point else 8,
        )
        for side in ("good", "bad")
    }
    if is_point:
        bank = build_calibration_bank(
            *[
                model.capture_ara_module_io(artifacts.fit_prompts[side])
                for side in ("good", "bad")
            ]
        )
    else:
        bank = build_trajectory_bank(
            *[
                capture_fixed_sequences(
                    model, model.ara_targets, sequences[side]
                )
                for side in ("good", "bad")
            ]
        )
    manifest = {
        "protocol_hash": artifacts.protocol["protocol_hash"],
        "base_identity": digest(artifacts.protocol["model_identity"]),
        "sequences": {
            side: [vars(row) for row in rows]
            for side, rows in sequences.items()
        },
        "blocks": [
            {
                "key": vars(target.key),
                "name": target.full_name,
                "shape": [target.out_features, target.in_features],
            }
            for target in model.ara_targets
        ],
        "steps": [0] if is_point else list(range(8)),
        "capture_mode": "frozen-reference",
    }
    return bank, manifest


def _apply_legacy_module(target, calibration, parameters, method):
    from .ara import (
        ARAComponentParameters,
        ARAOptimizerConfig,
        optimize_ara_module,
    )
    from .ara_trajectory import (
        TrajectoryModuleParameters,
        TrajectoryOptimizerConfig,
        optimize_trajectory_module,
    )

    a, b = get_lora_factors(target)
    strength = (
        parameters.mlp_strength
        if target.key.component == "mlp.down_proj"
        else parameters.attn_strength
    )
    if method == "B1":
        local = ARAComponentParameters(
            strength, parameters.push_weight, parameters.margin
        )
        return optimize_ara_module(
            calibration, a, b, local, ARAOptimizerConfig()
        )
    local = TrajectoryModuleParameters(
        strength, parameters.push_weight, parameters.margin, 1.0
    )
    return optimize_trajectory_module(
        calibration, a, b, local, TrajectoryOptimizerConfig()
    )


def apply_reference_trial(model, artifacts, parameters):
    """使用相同参数空间直接调用已有求解器，保留其数值行为。"""
    from dataclasses import asdict

    before = tensor_identity(snapshot_factors(model.ara_targets))
    bank, manifest = _capture_legacy_reference(model, artifacts)
    statistics = {}
    targets = tuple(
        target
        for group in _layer_blocks(model.ara_targets, parameters, 8)
        for target in group
    )
    for target in targets:
        artifacts.check_budget()
        statistics[target.full_name] = asdict(
            _apply_legacy_module(
                target, bank[target.key], parameters, artifacts.config.method_id
            )
        )
    after = tensor_identity(snapshot_factors(model.ara_targets))
    manifest.update(captured_state_hash=before, evaluation_state_hash=before)
    return [
        {
            "sweep": 0,
            "block_id": 0,
            "accepted": True,
            "rejection_reason": None,
            "before_adapter_hash": before,
            "after_adapter_hash": after,
            "bank_manifest": manifest,
            "capture_manifest_hash": digest(manifest),
            "local_statistics": statistics,
        }
    ]


def run_paired_ablation(context, settings):
    """消融复用父候选参数，只在机制开发角色评估，单独记账。"""
    from .ara_research_schema import CandidateLock, read_json

    config = settings.ara_v3
    if not config.parent_candidate_lock_path:
        raise ValueError("配对消融必须提供父候选锁文件")
    parent = read_json(config.parent_candidate_lock_path)
    if digest(parent) != config.parent_candidate_lock_hash:
        raise ValueError("配对消融父候选锁 hash 不匹配")
    lock = CandidateLock.model_validate(parent)
    expected = "S1" if config.method_id == "F1" else "S2"
    if lock.method_id != expected or lock.seed != settings.seed:
        raise ValueError("配对消融父方法或 seed 不匹配")
    if lock.protocol_hash != context["protocol"]["protocol_hash"]:
        raise ValueError("配对消融不能修改父候选数据协议")
    evidence = context["apply"](lock.parameters, lock.attempt, "ablation")
    semantics = context["semantic"](evidence)
    result = {
        "schema_version": "cara-research-ablation-v3",
        "phase": "search",
        "research_status": "not_run",
        "method_id": config.method_id,
        "parent_candidate_lock_hash": config.parent_candidate_lock_hash,
        "trial": evidence,
        "mechanism_semantics": semantics,
        "parent_controls": _parent_controls(context, lock),
        "gpu_hours": context["budget"].elapsed() * 2 / 3600,
    }
    write_json(
        Path(context["run_dir"]) / "ablation.json", result, immutable=True
    )
    return result, 0


def _parent_controls(context, lock):
    from .research_evaluation import run_mechanism_controls

    factors = torch.load(
        lock.final_snapshot_path, map_location="cpu", weights_only=True
    )
    if tensor_identity(factors) != lock.final_snapshot_hash:
        raise ValueError("机制对照父候选快照已改变")
    return run_mechanism_controls(
        context["model"],
        factors,
        {
            "role": "mechanism-development",
            "evaluate": context["mechanism_evaluate"],
            "check_budget": context["check_budget"],
        },
    )
