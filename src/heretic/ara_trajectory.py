# SPDX-License-Identifier: AGPL-3.0-or-later
"""Teacher-forced trajectory capture and local CARA optimization."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import torch
from torch import Tensor

from . import artifact_schema
from .ara import ARAOptimizationError, BALANCE_WEIGHT, ModuleKey, TargetModule

build_trajectory_manifest = artifact_schema.build_trajectory_manifest
canonical_manifest_sha256 = artifact_schema.canonical_sha256


class TrajectoryNonFiniteError(ARAOptimizationError):
    """Explicit non-finite trajectory failure used by study health accounting."""


class TrajectoryRuntimeGuardError(ARAOptimizationError):
    """Explicit device/resource guard failure with a fixed health category."""

    def __init__(self, category: str, message: str, key: ModuleKey | None = None):
        super().__init__("runtime-guard", message, key)
        self.category = category


def estimate_capture_gib(
    targets: Sequence[TargetModule],
    prompts_per_side: int,
    trajectory_tokens: int,
) -> float:
    """Estimate FP32 good/bad trajectory-bank storage before capture."""
    elements = sum(target.in_features + target.out_features for target in targets)
    observations = 2 * prompts_per_side * trajectory_tokens
    return observations * elements * 4 / 1024**3


@dataclass(frozen=True)
class TrajectoryObservation:
    """CPU FP32 module observations annotated with prompt and generation step."""

    inputs: Tensor
    outputs: Tensor
    prompt_indices: Tensor
    step_indices: Tensor
    weights: Tensor

    def validate(self) -> None:
        """Validate shapes, dtypes, finiteness, and prompt-normalized weights."""
        count = self.inputs.shape[0]
        fields = (self.outputs, self.prompt_indices, self.step_indices, self.weights)
        if self.inputs.ndim != 2 or self.outputs.ndim != 2:
            raise ARAOptimizationError(
                "trajectory-validation", "observations must be matrices"
            )
        if any(value.shape[0] != count for value in fields):
            raise ARAOptimizationError(
                "trajectory-validation", "observation lengths differ"
            )
        _validate_observation_indices(self, count)
        if self.inputs.device.type != "cpu" or self.outputs.device.type != "cpu":
            raise ARAOptimizationError(
                "trajectory-validation", "observations must remain on CPU"
            )
        if self.inputs.dtype != torch.float32 or self.outputs.dtype != torch.float32:
            raise ARAOptimizationError(
                "trajectory-validation", "observations must use FP32"
            )
        if (
            not torch.isfinite(self.inputs).all()
            or not torch.isfinite(self.outputs).all()
        ):
            raise TrajectoryNonFiniteError(
                "trajectory-validation", "observations are non-finite"
            )
        if not torch.isfinite(self.weights).all():
            raise TrajectoryNonFiniteError(
                "trajectory-validation", "weights are non-finite"
            )
        if bool((self.weights <= 0).any()):
            raise ARAOptimizationError(
                "trajectory-validation", "weights must be positive"
            )
        for prompt_index in self.prompt_indices.unique():
            mask = self.prompt_indices == prompt_index
            if not torch.allclose(self.weights[mask].sum(), torch.tensor(1.0)):
                raise ARAOptimizationError(
                    "trajectory-validation", "prompt weights must sum to one"
                )


def _validate_observation_indices(
    observation: TrajectoryObservation,
    count: int,
) -> None:
    vectors = (
        observation.prompt_indices,
        observation.step_indices,
        observation.weights,
    )
    if count == 0 or any(value.ndim != 1 for value in vectors):
        raise ARAOptimizationError(
            "trajectory-validation", "observation vectors must be non-empty"
        )
    dtypes = (
        observation.prompt_indices.dtype,
        observation.step_indices.dtype,
        observation.weights.dtype,
    )
    if dtypes != (torch.int64, torch.int16, torch.float32):
        raise ARAOptimizationError(
            "trajectory-validation", "observation vector dtypes are invalid"
        )
    prompts = sorted(observation.prompt_indices.unique().tolist())
    if prompts != list(range(len(prompts))) or int(observation.step_indices.min()) < 0:
        raise ARAOptimizationError(
            "trajectory-validation", "trajectory indices are negative or non-contiguous"
        )
    for prompt_index in prompts:
        mask = observation.prompt_indices == prompt_index
        steps = sorted(observation.step_indices[mask].tolist())
        if steps != list(range(len(steps))):
            raise ARAOptimizationError(
                "trajectory-validation", "steps must be contiguous within each prompt"
            )


TrajectoryIO = dict[ModuleKey, TrajectoryObservation]


@dataclass(frozen=True)
class TrajectoryBoundaries:
    """Explicit selection coordinates for a teacher-forced forward batch."""

    positions: Tensor
    prompt_indices: Tensor
    step_indices: Tensor
    weights: Tensor


@dataclass(frozen=True)
class TokenizedTrajectoryBatch:
    """A deferred forward call whose hooks capture exactly one module invocation."""

    forward: Callable[[], Any]


TrajectoryBatches = Sequence[TokenizedTrajectoryBatch]
BoundaryBatches = Sequence[TrajectoryBoundaries]


@dataclass(frozen=True)
class TrajectoryCaptureConfig:
    """Capture limits for a trajectory protocol."""

    trajectory_tokens: int = 8


@dataclass(frozen=True)
class StepCalibration:
    """Good and bad tensors for one generation step."""

    good_inputs: Tensor
    good_outputs: Tensor
    good_weights: Tensor
    bad_inputs: Tensor
    bad_outputs: Tensor
    bad_weights: Tensor
    scale: float


@dataclass(frozen=True)
class TrajectoryCalibration:
    """Per-step calibration data for one target module."""

    key: ModuleKey
    steps: Mapping[int, StepCalibration]


@dataclass(frozen=True)
class TrajectoryModuleParameters:
    """Local loss and deployment coordinates for one module family."""

    strength: float
    push_weight: float
    margin: float
    deployment_gain: float


@dataclass(frozen=True)
class TrajectoryOptimizerConfig:
    """Optimizer and deployment safety limits."""

    max_iter: int = 20
    history_size: int = 10
    temperature: float = 0.10
    max_good_delta_rms: float = 0.60
    max_singular_value: float = 8.0


@dataclass(frozen=True)
class TrajectoryLossTerms:
    """Finite scalar terms in the step-aware local objective."""

    keep: Tensor
    pull: Tensor
    push: Tensor
    balance: Tensor
    total: Tensor


@dataclass(frozen=True)
class TrajectoryOptimizationStats:
    """Post-deployment health statistics for one optimized module."""

    loss: float
    good_delta_ratio: float
    bad_delta_ratio: float
    max_singular_value: float
    deployment_gain: float


@dataclass(frozen=True)
class ContinuationRecord:
    """Non-textual identity of one generated base continuation."""

    sha256: str
    length: int
    ended_with_eos: bool


def continuation_record(
    token_ids: Sequence[int], eos_token_id: int | None
) -> ContinuationRecord:
    """Create a canonical token identity without persisting decoded text."""
    if not token_ids:
        raise ARAOptimizationError(
            "trajectory-generation", "continuation must contain at least one token"
        )
    tensor = torch.tensor(token_ids, dtype=torch.int64)
    digest = hashlib.sha256(tensor.numpy().tobytes()).hexdigest()
    return ContinuationRecord(
        sha256=digest,
        length=len(token_ids),
        ended_with_eos=eos_token_id is not None and token_ids[-1] == eos_token_id,
    )


def valid_continuation_tokens(
    generated: Sequence[int],
    eos_token_id: int | None,
    limit: int,
) -> tuple[int, ...]:
    """Keep through the first EOS and discard following generated padding."""
    result = []
    for token in generated[:limit]:
        result.append(int(token))
        if eos_token_id is not None and token == eos_token_id:
            break
    if not result:
        raise ARAOptimizationError(
            "trajectory-generation", "generation produced no continuation token"
        )
    return tuple(result)


def prompt_step_weights(
    lengths: Sequence[int], decay: float
) -> tuple[Tensor, Tensor, Tensor]:
    """Build prompt indices, step indices, and per-prompt normalized weights."""
    if not 0 < decay <= 1:
        raise ValueError("trajectory decay must be in (0, 1]")
    prompt_indices = []
    step_indices = []
    weights = []
    for prompt_index, length in enumerate(lengths):
        if length <= 0:
            raise ValueError("each prompt needs at least one valid step")
        raw = torch.tensor([decay**step for step in range(length)])
        normalized = raw / raw.sum()
        prompt_indices.extend([prompt_index] * length)
        step_indices.extend(range(length))
        weights.extend(normalized.tolist())
    return (
        torch.tensor(prompt_indices, dtype=torch.int64),
        torch.tensor(step_indices, dtype=torch.int16),
        torch.tensor(weights, dtype=torch.float32),
    )


def causal_capture_positions(
    attention_mask: Tensor,
    prompt_lengths: Sequence[int],
    continuation_lengths: Sequence[int],
) -> Tensor:
    """Return padded ``[row, position]`` coordinates used to predict each step."""
    if attention_mask.ndim != 2:
        raise ValueError("attention_mask must be two-dimensional")
    if (
        len(prompt_lengths) != attention_mask.shape[0]
        or len(continuation_lengths) != attention_mask.shape[0]
    ):
        raise ValueError("boundary lengths do not match the batch")
    positions = []
    width = attention_mask.shape[1]
    for row, (prompt_length, continuation_length) in enumerate(
        zip(prompt_lengths, continuation_lengths)
    ):
        if prompt_length <= 0 or continuation_length <= 0:
            raise ValueError("prompt and continuation lengths must be positive")
        full_length = prompt_length + max(0, continuation_length - 1)
        padding = width - full_length
        if padding < 0 or int(attention_mask[row].sum()) != full_length:
            raise ValueError("attention mask does not match unpadded lengths")
        positions.extend(
            (row, padding + prompt_length - 1 + step)
            for step in range(continuation_length)
        )
    return torch.tensor(positions, dtype=torch.int64)


def _captured_tensor(value: Any, positions: Tensor) -> Tensor:
    tensor = value[0] if isinstance(value, tuple) else value
    if not isinstance(tensor, Tensor) or tensor.ndim != 3:
        raise ARAOptimizationError(
            "trajectory-capture", "target module I/O must be a rank-three tensor"
        )
    coordinates = positions.to(tensor.device)
    rows, columns = coordinates[:, 0], coordinates[:, 1]
    return tensor.detach()[rows, columns].to(device="cpu", dtype=torch.float32)


def _validate_capture_lengths(
    tokenized_batches: TrajectoryBatches, boundaries: BoundaryBatches
) -> None:
    if len(tokenized_batches) != len(boundaries):
        raise ValueError("tokenized batches and boundaries must align")


def _capture_layout(
    boundaries: BoundaryBatches,
) -> tuple[Tensor, Tensor, Tensor, list[Tensor]]:
    """Build step-major metadata and batch-to-storage row mappings."""
    if not boundaries:
        raise ValueError("trajectory capture requires at least one batch")
    counts = [current.positions.shape[0] for current in boundaries]
    for current, count in zip(boundaries, counts):
        annotations = (
            current.prompt_indices,
            current.step_indices,
            current.weights,
        )
        if current.positions.ndim != 2 or current.positions.shape[1] != 2:
            raise ValueError("capture positions must have shape [N, 2]")
        if any(value.ndim != 1 or value.shape[0] != count for value in annotations):
            raise ValueError("capture annotations must align with positions")
    prompt_indices = torch.cat([item.prompt_indices for item in boundaries])
    step_indices = torch.cat([item.step_indices for item in boundaries])
    weights = torch.cat([item.weights for item in boundaries])
    order = torch.argsort(step_indices.to(torch.int64), stable=True)
    inverse = torch.empty_like(order)
    inverse[order] = torch.arange(order.numel(), dtype=order.dtype)
    destinations = []
    offset = 0
    for count in counts:
        destinations.append(inverse[offset : offset + count])
        offset += count
    return prompt_indices[order], step_indices[order], weights[order], destinations


def _empty_capture(
    target: TargetModule,
    metadata: tuple[Tensor, Tensor, Tensor],
) -> TrajectoryObservation:
    """Preallocate one target's final CPU storage without a concatenation copy."""
    prompt_indices, step_indices, weights = metadata
    count = prompt_indices.shape[0]
    return TrajectoryObservation(
        inputs=torch.empty((count, target.in_features), dtype=torch.float32),
        outputs=torch.empty((count, target.out_features), dtype=torch.float32),
        prompt_indices=prompt_indices,
        step_indices=step_indices,
        weights=weights,
    )


def _capture_hook(
    target: TargetModule,
    captured: TrajectoryIO,
    active: list[tuple[TrajectoryBoundaries, Tensor]],
    calls: dict[ModuleKey, int],
) -> Callable[[Any, tuple[Any, ...], Any], None]:
    """Create one hook that writes directly into final step-major storage."""

    def hook(module: Any, inputs: tuple[Any, ...], output: Any) -> None:
        del module
        if not active:
            raise ARAOptimizationError(
                "trajectory-capture", "hook fired outside capture"
            )
        calls[target.key] = calls.get(target.key, 0) + 1
        current, destination = active[-1]
        observation = captured[target.key]
        observation.inputs.index_copy_(
            0, destination, _captured_tensor(inputs[0], current.positions)
        )
        observation.outputs.index_copy_(
            0, destination, _captured_tensor(output, current.positions)
        )

    return hook


def capture_trajectory_io(
    targets: Sequence[TargetModule],
    tokenized_batches: TrajectoryBatches,
    boundaries: BoundaryBatches,
    config: TrajectoryCaptureConfig,
) -> TrajectoryIO:
    """Capture CPU FP32 target-module inputs and outputs for each trajectory step."""
    _validate_capture_lengths(tokenized_batches, boundaries)
    if len({target.key for target in targets}) != len(targets):
        raise ValueError("trajectory target keys must be unique")
    prompt_indices, step_indices, weights, destinations = _capture_layout(boundaries)
    metadata = (prompt_indices, step_indices, weights)
    captured = {target.key: _empty_capture(target, metadata) for target in targets}
    active: list[tuple[TrajectoryBoundaries, Tensor]] = []
    calls: dict[ModuleKey, int] = {}
    handles = []
    try:
        for target in targets:
            hook = _capture_hook(target, captured, active, calls)
            handles.append(target.module.register_forward_hook(hook))
        for batch, current, destination in zip(
            tokenized_batches, boundaries, destinations
        ):
            calls.clear()
            active.append((current, destination))
            try:
                batch.forward()
            finally:
                active.pop()
            if any(calls.get(target.key, 0) != 1 for target in targets):
                raise ARAOptimizationError(
                    "trajectory-capture",
                    "each target must run exactly once per forward",
                )
    finally:
        for handle in handles:
            handle.remove()
    for key, observation in captured.items():
        observation.validate()
        if int(observation.step_indices.max()) >= config.trajectory_tokens:
            raise ARAOptimizationError(
                "trajectory-capture", "step exceeds trajectory token limit", key
            )
    return captured


def _step_scale(outputs: Tensor) -> float:
    centered = outputs - outputs.mean(dim=0, keepdim=True)
    return max(1e-6, float(centered.square().mean()))


def _step_rows(observation: TrajectoryObservation, step: int) -> slice | Tensor:
    """Select one step, preserving storage when capture rows are step-major."""
    indices = torch.nonzero(observation.step_indices == step).flatten()
    if indices.numel() == 0:
        return indices
    start, stop = int(indices[0]), int(indices[-1]) + 1
    if torch.equal(indices, torch.arange(start, stop, dtype=indices.dtype)):
        return slice(start, stop)
    return indices


def build_trajectory_bank(
    good_io: Mapping[ModuleKey, TrajectoryObservation],
    bad_io: Mapping[ModuleKey, TrajectoryObservation],
) -> dict[ModuleKey, TrajectoryCalibration]:
    """Build strict same-step good/bad calibration references."""
    if good_io.keys() != bad_io.keys():
        raise ARAOptimizationError(
            "trajectory-bank", "good and bad trajectory targets differ"
        )
    result = {}
    for key in good_io:
        good, bad = good_io[key], bad_io[key]
        good.validate()
        bad.validate()
        step_ids = sorted(
            set(good.step_indices.tolist()) | set(bad.step_indices.tolist())
        )
        steps = {}
        for step in step_ids:
            good_rows = _step_rows(good, step)
            bad_rows = _step_rows(bad, step)
            good_count = good.inputs[good_rows].shape[0]
            bad_count = bad.inputs[bad_rows].shape[0]
            if good_count < 2 or bad_count < 2:
                raise ARAOptimizationError(
                    "trajectory-bank",
                    f"step {step} has fewer than two references",
                    key,
                )
            steps[step] = StepCalibration(
                good_inputs=good.inputs[good_rows],
                good_outputs=good.outputs[good_rows],
                good_weights=good.weights[good_rows],
                bad_inputs=bad.inputs[bad_rows],
                bad_outputs=bad.outputs[bad_rows],
                bad_weights=bad.weights[bad_rows],
                scale=_step_scale(good.outputs[good_rows]),
            )
        result[key] = TrajectoryCalibration(key=key, steps=steps)
    return result


def _soft_nearest(distances: Tensor, temperature: float) -> Tensor:
    normalizer = torch.log(torch.tensor(distances.shape[1], device=distances.device))
    return -temperature * (
        torch.logsumexp(-distances / temperature, dim=1) - normalizer
    )


def _updates(inputs: Tensor, lora_a: Tensor, lora_b: Tensor) -> Tensor:
    return (inputs @ lora_a.T) @ lora_b.T


def _step_losses(
    step: StepCalibration,
    lora_a: Tensor,
    lora_b: Tensor,
    parameters: TrajectoryModuleParameters,
    temperature: float,
) -> tuple[Tensor, Tensor, Tensor]:
    good_delta = _updates(step.good_inputs, lora_a, lora_b)
    bad_delta = _updates(step.bad_inputs, lora_a, lora_b)
    adapted_bad = step.bad_outputs + bad_delta
    denominator = step.bad_outputs.shape[1] * step.scale
    keep = good_delta.square().sum(dim=1) / denominator
    pull_distances = torch.cdist(adapted_bad, step.good_outputs).square() / denominator
    bad_distances = torch.cdist(adapted_bad, step.bad_outputs).square() / denominator
    pull = _soft_nearest(pull_distances, temperature)
    push_distance = _soft_nearest(bad_distances, temperature)
    push = temperature * torch.nn.functional.softplus(
        (parameters.margin - push_distance) / temperature
    )
    return keep, pull, push


def trajectory_loss(
    calibration: TrajectoryCalibration,
    lora_a: Tensor,
    lora_b: Tensor,
    parameters: TrajectoryModuleParameters,
    temperature: float,
) -> TrajectoryLossTerms:
    """Compute prompt-normalized, same-step CARA objective terms."""
    keep_terms = []
    pull_terms = []
    push_terms = []
    good_weight_totals = []
    bad_weight_totals = []
    for step in calibration.steps.values():
        keep_row, pull_row, push_row = _step_losses(
            step, lora_a, lora_b, parameters, temperature
        )
        keep_terms.append((keep_row * step.good_weights).sum())
        pull_terms.append((pull_row * step.bad_weights).sum())
        push_terms.append((push_row * step.bad_weights).sum())
        good_weight_totals.append(step.good_weights.sum())
        bad_weight_totals.append(step.bad_weights.sum())
    keep = torch.stack(keep_terms).sum() / torch.stack(good_weight_totals).sum()
    pull = torch.stack(pull_terms).sum() / torch.stack(bad_weight_totals).sum()
    push = torch.stack(push_terms).sum() / torch.stack(bad_weight_totals).sum()
    balance = (lora_a @ lora_a.T - lora_b.T @ lora_b).square().mean()
    total = (
        keep
        + parameters.strength * (pull + parameters.push_weight * push)
        + BALANCE_WEIGHT * balance
    )
    return TrajectoryLossTerms(keep, pull, push, balance, total)


def _canonical_factors(lora_a: Tensor, lora_b: Tensor) -> tuple[Tensor, Tensor, Tensor]:
    qb, rb = torch.linalg.qr(lora_b, mode="reduced")
    qa, ra = torch.linalg.qr(lora_a.T, mode="reduced")
    left, singular, right = torch.linalg.svd(rb @ ra.T, full_matrices=False)
    root = singular.clamp_min(0).sqrt()
    canonical_b = (qb @ left) * root.unsqueeze(0)
    canonical_a = root.unsqueeze(1) * (right @ qa.T)
    return canonical_a, canonical_b, singular


def _deployment_ratios(
    calibration: TrajectoryCalibration,
    lora_a: Tensor,
    lora_b: Tensor,
) -> tuple[float, float]:
    good_values = []
    bad_values = []
    good_weights = []
    bad_weights = []
    for step in calibration.steps.values():
        good_delta = _updates(step.good_inputs, lora_a, lora_b)
        bad_delta = _updates(step.bad_inputs, lora_a, lora_b)
        good_ratio = good_delta.square().mean(
            dim=1
        ).sqrt() / step.good_outputs.square().mean(dim=1).sqrt().clamp_min(1e-6)
        bad_ratio = bad_delta.square().mean(
            dim=1
        ).sqrt() / step.bad_outputs.square().mean(dim=1).sqrt().clamp_min(1e-6)
        good_values.append((good_ratio * step.good_weights).sum())
        bad_values.append((bad_ratio * step.bad_weights).sum())
        good_weights.append(step.good_weights.sum())
        bad_weights.append(step.bad_weights.sum())
    good = torch.stack(good_values).sum() / torch.stack(good_weights).sum()
    bad = torch.stack(bad_values).sum() / torch.stack(bad_weights).sum()
    return float(good), float(bad)


def _calibration_to_device(
    calibration: TrajectoryCalibration,
    device: torch.device,
) -> TrajectoryCalibration:
    steps = {
        index: StepCalibration(
            good_inputs=step.good_inputs.to(device),
            good_outputs=step.good_outputs.to(device),
            good_weights=step.good_weights.to(device),
            bad_inputs=step.bad_inputs.to(device),
            bad_outputs=step.bad_outputs.to(device),
            bad_weights=step.bad_weights.to(device),
            scale=step.scale,
        )
        for index, step in calibration.steps.items()
    }
    return TrajectoryCalibration(calibration.key, steps)


def _validate_finite_loss(loss: Tensor, key: ModuleKey, label: str) -> None:
    if not torch.isfinite(loss):
        raise TrajectoryNonFiniteError(
            "trajectory-optimizer", f"{label} loss is non-finite", key
        )


def _run_lbfgs(
    calibration: TrajectoryCalibration,
    lora_a: Tensor,
    lora_b: Tensor,
    parameters: TrajectoryModuleParameters,
    config: TrajectoryOptimizerConfig,
) -> tuple[Tensor, Tensor]:
    initial = trajectory_loss(
        calibration, lora_a, lora_b, parameters, config.temperature
    ).total
    _validate_finite_loss(initial, calibration.key, "initial")
    lora_a.requires_grad_(True)
    lora_b.requires_grad_(True)
    optimizer = torch.optim.LBFGS(
        [lora_a, lora_b],
        max_iter=config.max_iter,
        history_size=config.history_size,
        line_search_fn="strong_wolfe",
    )

    def closure() -> Tensor:
        optimizer.zero_grad()
        loss = trajectory_loss(
            calibration, lora_a, lora_b, parameters, config.temperature
        ).total
        if not torch.isfinite(loss):
            raise TrajectoryNonFiniteError(
                "trajectory-optimizer",
                "loss is non-finite",
                calibration.key,
            )
        loss.backward()
        return loss

    with torch.enable_grad():
        optimizer.step(closure)
    final = trajectory_loss(
        calibration, lora_a, lora_b, parameters, config.temperature
    ).total
    _validate_finite_loss(final, calibration.key, "final")
    if float(final) > float(initial) + 1e-6:
        raise ARAOptimizationError(
            "trajectory-optimizer", "objective did not decrease", calibration.key
        )
    return initial, final


def _deploy_canonical_factors(
    calibration: TrajectoryCalibration,
    lora_a: Tensor,
    lora_b: Tensor,
    parameters: TrajectoryModuleParameters,
) -> float:
    if not torch.isfinite(lora_a).all() or not torch.isfinite(lora_b).all():
        raise TrajectoryNonFiniteError(
            "trajectory-canonicalization",
            "optimized factors are non-finite",
            calibration.key,
        )
    canonical_a, canonical_b, singular = _canonical_factors(lora_a, lora_b)
    if not all(
        torch.isfinite(value).all() for value in (canonical_a, canonical_b, singular)
    ):
        raise TrajectoryNonFiniteError(
            "trajectory-canonicalization",
            "canonical factors are non-finite",
            calibration.key,
        )
    probe = next(iter(calibration.steps.values())).bad_inputs
    before = _updates(probe, lora_a, lora_b)
    after = _updates(probe, canonical_a, canonical_b)
    if not torch.allclose(before, after, rtol=1e-6, atol=1e-7):
        raise ARAOptimizationError(
            "trajectory-canonicalization",
            "canonical factors changed the effective update",
            calibration.key,
        )
    with torch.no_grad():
        lora_a.copy_(canonical_a)
        lora_b.copy_(canonical_b * parameters.deployment_gain)
    return float(singular.max()) * parameters.deployment_gain


def _validated_deployment_stats(
    calibration: TrajectoryCalibration,
    lora_a: Tensor,
    lora_b: Tensor,
    maximum: float,
    config: TrajectoryOptimizerConfig,
) -> tuple[float, float, float]:
    good_ratio, bad_ratio = _deployment_ratios(calibration, lora_a, lora_b)
    if not all(torch.isfinite(torch.tensor((good_ratio, bad_ratio, maximum)))):
        raise TrajectoryNonFiniteError(
            "trajectory-deployment",
            "deployment statistics are non-finite",
            calibration.key,
        )
    if good_ratio > config.max_good_delta_rms:
        raise ARAOptimizationError(
            "trajectory-deployment", "good delta guard exceeded", calibration.key
        )
    if maximum > config.max_singular_value:
        raise ARAOptimizationError(
            "trajectory-deployment", "singular value guard exceeded", calibration.key
        )
    return good_ratio, bad_ratio, maximum


def optimize_trajectory_module(
    calibration: TrajectoryCalibration,
    lora_a: Tensor,
    lora_b: Tensor,
    parameters: TrajectoryModuleParameters,
    config: TrajectoryOptimizerConfig,
) -> TrajectoryOptimizationStats:
    """Optimize one factor pair transactionally and enforce deployment guards."""
    if lora_a.dtype != torch.float32 or lora_b.dtype != torch.float32:
        raise ARAOptimizationError(
            "trajectory-optimizer", "LoRA factors must use FP32", calibration.key
        )
    device_calibration = _calibration_to_device(calibration, lora_a.device)
    original_a = lora_a.detach().clone()
    original_b = lora_b.detach().clone()
    try:
        _, final = _run_lbfgs(device_calibration, lora_a, lora_b, parameters, config)
        maximum = _deploy_canonical_factors(
            device_calibration, lora_a, lora_b, parameters
        )
        good_ratio, bad_ratio, maximum = _validated_deployment_stats(
            device_calibration, lora_a, lora_b, maximum, config
        )
        return TrajectoryOptimizationStats(
            loss=float(final),
            good_delta_ratio=good_ratio,
            bad_delta_ratio=bad_ratio,
            max_singular_value=maximum,
            deployment_gain=parameters.deployment_gain,
        )
    except BaseException:
        with torch.no_grad():
            lora_a.copy_(original_a)
            lora_b.copy_(original_b)
        raise
    finally:
        lora_a.requires_grad_(False)
        lora_b.requires_grad_(False)
