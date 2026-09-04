# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026  Philipp Emanuel Weidmann <pew@worldwidemann.com> + contributors

"""Tensor capture, deterministic adapter state, and CARA optimization."""

import hashlib
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any, TypeAlias, cast

import torch
import torch.nn.functional as F
from torch import Tensor
from torch.nn import Module

from .utils import Prompt, batchify

SUPPORTED_COMPONENTS = ("attn.o_proj", "mlp.down_proj")
CAPTURE_PROTOCOL = "cara-capture-v1"
OPTIMIZER_SCHEMA = "cara-lbfgs-v1"
BALANCE_WEIGHT = 1e-4
MIN_SCALE = 1e-6


@dataclass(frozen=True, order=True)
class ModuleKey:
    """Stable identity of one target projection within the language model."""

    layer_index: int
    component: str
    module_index: int


@dataclass(frozen=True)
class TargetModule:
    """Runtime record for one PEFT-wrapped projection module."""

    key: ModuleKey
    full_name: str
    module: Module
    in_features: int
    out_features: int


@dataclass(frozen=True)
class ModuleObservation:
    """Last-prefill-token inputs and outputs for a target module."""

    inputs: Tensor
    outputs: Tensor


ModuleIO: TypeAlias = dict[ModuleKey, ModuleObservation]
GenerateCallback: TypeAlias = Callable[[list[Prompt]], Any]


@dataclass(frozen=True)
class ARACaptureConfig:
    """Settings for deterministic target-module I/O capture."""

    batch_size: int = 1
    require_all_targets: bool = True

    def __post_init__(self) -> None:
        if self.batch_size <= 0:
            raise ValueError("capture batch_size must be positive")


@dataclass(frozen=True)
class ARACalibration:
    """Clean good/bad observations used by one local CARA objective."""

    key: ModuleKey
    good_inputs: Tensor
    good_outputs: Tensor
    bad_inputs: Tensor
    bad_outputs: Tensor
    scale: Tensor
    balance_reference: Tensor | None = None


@dataclass(frozen=True)
class ARAComponentParameters:
    """Steering parameters applied to one logical component."""

    strength: float
    push_weight: float
    margin: float

    def __post_init__(self) -> None:
        if self.strength < 0:
            raise ValueError("component strength must be non-negative")
        if not 0 <= self.push_weight <= 2:
            raise ValueError("push_weight must be in [0, 2]")
        if not 0.25 <= self.margin <= 4:
            raise ValueError("margin must be in [0.25, 4]")


@dataclass(frozen=True)
class ARAParameters:
    """Resolved, immutable parameters for one CARA trial."""

    start_layer_index: int
    end_layer_index: int
    components: dict[str, ARAComponentParameters]

    def __post_init__(self) -> None:
        if self.start_layer_index < 0:
            raise ValueError("start_layer_index must be non-negative")
        if self.end_layer_index <= self.start_layer_index:
            raise ValueError("end_layer_index must be greater than start_layer_index")


@dataclass(frozen=True)
class ARALossTerms:
    """Named scalar terms of the scale-calibrated CARA objective."""

    total: Tensor
    keep: Tensor
    pull: Tensor
    push: Tensor
    balance: Tensor


@dataclass(frozen=True)
class ARAOptimizerConfig:
    """Versioned L-BFGS configuration for local LoRA optimization."""

    max_iter: int = 20
    history_size: int = 10
    temperature: float = 0.10
    optimizer_schema: str = OPTIMIZER_SCHEMA

    def __post_init__(self) -> None:
        if self.max_iter <= 0 or self.history_size <= 0:
            raise ValueError("L-BFGS iteration and history sizes must be positive")
        if self.temperature <= 0:
            raise ValueError("softmin temperature must be positive")
        if self.optimizer_schema != OPTIMIZER_SCHEMA:
            raise ValueError(f"unsupported optimizer schema: {self.optimizer_schema}")

    @property
    def max_eval(self) -> int:
        """Maximum closure evaluations derived from the iteration limit."""

        return max(25, math.ceil(1.25 * self.max_iter))


@dataclass(frozen=True)
class ARAOptimizationStats:
    """Numerical and performance diagnostics for one optimized module."""

    initial_loss: float
    final_loss: float
    closure_calls: int
    good_samples: int
    bad_samples: int
    lora_a_norm: float
    lora_b_norm: float
    max_singular_value: float
    canonicalization_max_output_error: float
    elapsed_seconds: float


@dataclass(frozen=True)
class ARAOptimizationSummary:
    """Journal-safe aggregate of all module optimizations in a trial."""

    processed_modules: int
    skipped_modules: int
    failed_modules: int
    median_initial_loss: float
    median_final_loss: float
    max_final_loss: float
    elapsed_seconds: float


@dataclass(frozen=True)
class AdapterInitialState:
    """Complete deterministic LoRA A/B state stored on CPU in FP32."""

    tensors: dict[str, Tensor]
    ordered_parameter_names: tuple[str, ...]


@dataclass(frozen=True)
class CalibrationManifest:
    """Auditable identities for the sampled calibration prompt rows."""

    good_indices: tuple[int, ...]
    bad_indices: tuple[int, ...]
    good_prompt_sha256: tuple[str, ...]
    bad_prompt_sha256: tuple[str, ...]
    seed: int
    capture_batch_size: int
    protocol_version: str = CAPTURE_PROTOCOL
    good_dataset: str | None = None
    good_revision: str | None = None
    bad_dataset: str | None = None
    bad_revision: str | None = None


@dataclass(frozen=True)
class ARAArtifacts:
    """Immutable calibration and reset artifacts shared by CARA trials."""

    calibration_bank: dict[ModuleKey, ARACalibration]
    adapter_initial_state: AdapterInitialState
    calibration_manifest: CalibrationManifest
    model_fingerprint: str
    study_fingerprint: str
    targets: tuple[TargetModule, ...]
    optimizer_config: ARAOptimizerConfig


class ARAOptimizationError(RuntimeError):
    """Expected, trial-prunable CARA optimization failure."""

    def __init__(self, stage: str, message: str, key: ModuleKey | None = None):
        super().__init__(message)
        self.stage = stage
        self.key = key

    def to_record(self) -> dict[str, Any]:
        """Serialize the failure without tensors or prompt content."""

        return {
            "type": type(self).__name__,
            "stage": self.stage,
            "module": asdict(self.key) if self.key is not None else None,
            "message": str(self),
        }


def _factor_pair(target: TargetModule) -> tuple[Tensor, Tensor]:
    module = target.module
    try:
        lora_a = cast(Mapping[str, Module], getattr(module, "lora_A"))["default"]
        lora_b = cast(Mapping[str, Module], getattr(module, "lora_B"))["default"]
        weight_a = cast(Tensor, getattr(lora_a, "weight"))
        weight_b = cast(Tensor, getattr(lora_b, "weight"))
    except (AttributeError, KeyError, TypeError) as error:
        raise ARAOptimizationError(
            "adapter-validation",
            f"{target.full_name} has no default LoRA A/B factors",
            target.key,
        ) from error
    active = list(getattr(module, "active_adapters", ()))
    scaling = getattr(module, "scaling", {}).get("default")
    use_dora = getattr(module, "use_dora", {}).get("default", False)
    if active != ["default"] or scaling != 1 or use_dora:
        raise ARAOptimizationError(
            "adapter-validation",
            "CARA requires the active default adapter with unit scaling and no DoRA",
            target.key,
        )
    if getattr(module, "fan_in_fan_out", False):
        raise ARAOptimizationError(
            "adapter-validation",
            "CARA does not support fan-in/fan-out LoRA",
            target.key,
        )
    _validate_factors(target, weight_a, weight_b)
    return weight_a, weight_b


def get_lora_factors(target: TargetModule) -> tuple[Tensor, Tensor]:
    """Return validated default LoRA A/B factors for a target module."""
    return _factor_pair(target)


def _validate_factors(target: TargetModule, lora_a: Tensor, lora_b: Tensor) -> None:
    if lora_a.dtype != torch.float32 or lora_b.dtype != torch.float32:
        raise ARAOptimizationError(
            "adapter-validation", "CARA requires FP32 LoRA factors", target.key
        )
    if lora_a.device != lora_b.device:
        raise ARAOptimizationError(
            "adapter-validation", "LoRA factors are on different devices", target.key
        )
    if lora_a.ndim != 2 or lora_b.ndim != 2:
        raise ARAOptimizationError(
            "adapter-validation", "LoRA factors must be matrices", target.key
        )
    expected = (target.in_features, target.out_features)
    actual = (lora_a.shape[1], lora_b.shape[0])
    if actual != expected or lora_a.shape[0] != lora_b.shape[1]:
        raise ARAOptimizationError(
            "adapter-validation", f"LoRA factor shape mismatch: {actual}", target.key
        )


def _factor_names(target: TargetModule) -> tuple[str, str]:
    prefix = target.full_name
    return (
        f"{prefix}.lora_A.default.weight",
        f"{prefix}.lora_B.default.weight",
    )


def _module_seed(seed: int, full_name: str) -> int:
    digest = hashlib.sha256(f"{seed}:{full_name}".encode()).digest()
    return int.from_bytes(digest[-8:], "big")


def snapshot_adapter_state(
    targets: tuple[TargetModule, ...], seed: int
) -> AdapterInitialState:
    """Initialize all LoRA factors deterministically and snapshot them on CPU."""

    tensors: dict[str, Tensor] = {}
    for target in targets:
        lora_a, lora_b = _factor_pair(target)
        generator = torch.Generator(device="cpu")
        generator.manual_seed(_module_seed(seed, target.full_name))
        initial_a = torch.empty(lora_a.shape, dtype=torch.float32, device="cpu")
        torch.nn.init.kaiming_uniform_(initial_a, a=math.sqrt(5), generator=generator)
        with torch.no_grad():
            lora_a.copy_(initial_a.to(lora_a.device))
            lora_b.zero_()
        lora_a.requires_grad_(False)
        lora_b.requires_grad_(False)
        name_a, name_b = _factor_names(target)
        tensors[name_a] = lora_a.detach().to("cpu", torch.float32).clone()
        tensors[name_b] = lora_b.detach().to("cpu", torch.float32).clone()
    names = tuple(sorted(tensors))
    return AdapterInitialState(tensors=tensors, ordered_parameter_names=names)


def restore_adapter_state(
    targets: tuple[TargetModule, ...], state: AdapterInitialState
) -> None:
    """Restore every LoRA A/B factor and clear gradient state."""

    expected_names = tuple(
        sorted(name for target in targets for name in _factor_names(target))
    )
    if expected_names != state.ordered_parameter_names:
        raise ARAOptimizationError(
            "adapter-restore", "adapter module names differ from initial snapshot"
        )
    for target in targets:
        lora_a, lora_b = _factor_pair(target)
        name_a, name_b = _factor_names(target)
        with torch.no_grad():
            lora_a.copy_(state.tensors[name_a].to(lora_a.device))
            lora_b.copy_(state.tensors[name_b].to(lora_b.device))
        for factor in (lora_a, lora_b):
            factor.requires_grad_(False)
            factor.grad = None


def _capture_hook(
    target: TargetModule,
    observations: dict[ModuleKey, list[ModuleObservation]],
    calls: dict[ModuleKey, int],
) -> Callable[[Module, tuple[Any, ...], Any], None]:
    def hook(module: Module, inputs: tuple[Any, ...], output: Any) -> None:
        del module
        calls[target.key] += 1
        if calls[target.key] > 1:
            raise RuntimeError(f"{target.full_name} ran more than once in one batch")
        if not inputs or not isinstance(inputs[0], Tensor):
            raise TypeError(f"{target.full_name} did not receive a tensor input")
        if not isinstance(output, Tensor):
            raise TypeError(f"{target.full_name} did not return a tensor")
        observed = ModuleObservation(
            inputs=inputs[0][:, -1, :].detach().to("cpu", torch.float32),
            outputs=output[:, -1, :].detach().to("cpu", torch.float32),
        )
        observations[target.key].append(observed)

    return hook


def _capture_one_batch(
    targets: tuple[TargetModule, ...],
    prompts: list[Prompt],
    generate_one_token: GenerateCallback,
    observations: dict[ModuleKey, list[ModuleObservation]],
) -> None:
    calls = {target.key: 0 for target in targets}
    handles = []
    try:
        for target in targets:
            handles.append(
                target.module.register_forward_hook(
                    _capture_hook(target, observations, calls)
                )
            )
        generate_one_token(prompts)
        missing = [target.full_name for target in targets if calls[target.key] != 1]
        if missing:
            raise RuntimeError(
                "target modules not called exactly once: " + ", ".join(missing)
            )
    finally:
        for handle in handles:
            handle.remove()


def capture_module_io(
    targets: tuple[TargetModule, ...],
    prompts: list[Prompt],
    generate_one_token: GenerateCallback,
    config: ARACaptureConfig,
) -> ModuleIO:
    """Capture CPU FP32 last-prefill-token I/O for all target modules."""

    if not targets:
        raise ValueError("at least one target module is required")
    if not prompts:
        raise ValueError("at least one capture prompt is required")
    observations = {target.key: [] for target in targets}
    with torch.no_grad():
        for prompt_batch in batchify(prompts, config.batch_size):
            _capture_one_batch(targets, prompt_batch, generate_one_token, observations)
    result = {}
    for target in targets:
        values = observations[target.key]
        if config.require_all_targets and not values:
            raise RuntimeError(f"no observations captured for {target.full_name}")
        if values:
            result[target.key] = ModuleObservation(
                inputs=torch.cat([value.inputs for value in values]),
                outputs=torch.cat([value.outputs for value in values]),
            )
    return result


def _validate_calibration(
    calibration: ARACalibration, factors: tuple[Tensor, Tensor] | None = None
) -> None:
    tensors = (
        calibration.good_inputs,
        calibration.good_outputs,
        calibration.bad_inputs,
        calibration.bad_outputs,
    )
    if any(
        item.ndim != 2
        or item.dtype != torch.float32
        or item.device.type != "cpu"
        or not torch.isfinite(item).all()
        for item in tensors
    ):
        raise ARAOptimizationError(
            "calibration",
            "observations must be finite CPU FP32 matrices",
            calibration.key,
        )
    if (
        len(calibration.good_inputs) != len(calibration.good_outputs)
        or len(calibration.bad_inputs) != len(calibration.bad_outputs)
        or calibration.good_inputs.shape[1] != calibration.bad_inputs.shape[1]
        or calibration.good_outputs.shape[1] != calibration.bad_outputs.shape[1]
    ):
        raise ARAOptimizationError(
            "calibration", "observation shapes do not match", calibration.key
        )
    if (
        calibration.scale.dtype != torch.float32
        or calibration.scale.device.type != "cpu"
        or calibration.scale.ndim
        or not torch.isfinite(calibration.scale)
        or calibration.scale < MIN_SCALE
    ):
        raise ARAOptimizationError(
            "calibration", "scale must be a finite scalar", calibration.key
        )
    if factors and (
        calibration.good_inputs.shape[1] != factors[0].shape[1]
        or calibration.good_outputs.shape[1] != factors[1].shape[0]
    ):
        raise ARAOptimizationError(
            "calibration", "factor and observation shapes differ", calibration.key
        )


def build_calibration_bank(
    good_io: ModuleIO, bad_io: ModuleIO, *, require_all: bool = True
) -> dict[ModuleKey, ARACalibration]:
    """Pair good/bad observations and calculate each module activation scale."""

    if require_all and set(good_io) != set(bad_io):
        missing_good = sorted(set(bad_io) - set(good_io))
        missing_bad = sorted(set(good_io) - set(bad_io))
        raise ARAOptimizationError(
            "calibration", f"missing good={missing_good}, missing bad={missing_bad}"
        )
    bank = {}
    for key in sorted(set(good_io) & set(bad_io)):
        good = good_io[key]
        bad = bad_io[key]
        if min(len(good.inputs), len(bad.inputs)) < 2:
            raise ARAOptimizationError(
                "calibration", "requires at least two samples", key
            )
        centered = good.outputs - good.outputs.mean(dim=0)
        scale = centered.square().mean().clamp_min(MIN_SCALE)
        calibration = ARACalibration(
            key=key,
            good_inputs=good.inputs,
            good_outputs=good.outputs,
            bad_inputs=bad.inputs,
            bad_outputs=bad.outputs,
            scale=scale,
        )
        _validate_calibration(calibration)
        bank[key] = calibration
    return bank


def soft_neighbor_distance(
    queries: Tensor,
    references: Tensor,
    *,
    scale: Tensor,
    temperature: float,
) -> Tensor:
    """Compute a stable soft minimum of scale-normalized pairwise MSE."""

    if queries.ndim != 2 or references.ndim != 2:
        raise ValueError("queries and references must be rank-two tensors")
    if queries.shape[1] != references.shape[1] or len(references) == 0:
        raise ValueError("queries and references must have matching non-empty features")
    if temperature <= 0:
        raise ValueError("temperature must be positive")
    dimensions = queries.shape[1]
    squared = (
        queries.square().sum(dim=1, keepdim=True)
        + references.square().sum(dim=1).unsqueeze(0)
        - 2 * queries @ references.T
    )
    distances = (squared / (dimensions * scale)).clamp_min(0)
    normalizer = math.log(len(references))
    return -temperature * (
        torch.logsumexp(-distances / temperature, dim=1) - normalizer
    )


def _updated_outputs(
    inputs: Tensor, outputs: Tensor, lora_a: Tensor, lora_b: Tensor
) -> Tensor:
    return outputs + (inputs @ lora_a.T) @ lora_b.T


def _gram_balance(lora_a: Tensor, lora_b: Tensor) -> Tensor:
    return (lora_a @ lora_a.T - lora_b.T @ lora_b).square().mean()


def calculate_ara_loss(
    calibration: ARACalibration,
    lora_a: Tensor,
    lora_b: Tensor,
    parameters: ARAComponentParameters,
    *,
    temperature: float,
) -> ARALossTerms:
    """Calculate the bounded, scale-normalized CARA objective."""

    new_good = _updated_outputs(
        calibration.good_inputs, calibration.good_outputs, lora_a, lora_b
    )
    new_bad = _updated_outputs(
        calibration.bad_inputs, calibration.bad_outputs, lora_a, lora_b
    )
    keep = (new_good - calibration.good_outputs).square().mean() / calibration.scale
    pull = soft_neighbor_distance(
        new_bad,
        calibration.good_outputs,
        scale=calibration.scale,
        temperature=temperature,
    ).mean()
    bad_distance = soft_neighbor_distance(
        new_bad,
        calibration.bad_outputs,
        scale=calibration.scale,
        temperature=temperature,
    )
    push = (
        temperature * F.softplus((parameters.margin - bad_distance) / temperature)
    ).mean()
    gram = _gram_balance(lora_a, lora_b)
    reference = calibration.balance_reference
    if reference is None:
        reference = gram.detach().clamp_min(MIN_SCALE)
    balance = BALANCE_WEIGHT * gram / reference
    total = (
        keep + parameters.strength * (pull + parameters.push_weight * push) + balance
    )
    return ARALossTerms(total=total, keep=keep, pull=pull, push=push, balance=balance)


def _calibration_to_device(
    calibration: ARACalibration, device: torch.device
) -> ARACalibration:
    values = {
        name: cast(Tensor, getattr(calibration, name)).to(device)
        for name in (
            "good_inputs",
            "good_outputs",
            "bad_inputs",
            "bad_outputs",
            "scale",
        )
    }
    return replace(calibration, **values)


def _ensure_finite(stage: str, key: ModuleKey, **values: Tensor) -> None:
    invalid = [
        name for name, value in values.items() if not torch.isfinite(value).all()
    ]
    if invalid:
        raise ARAOptimizationError(stage, "non-finite " + ", ".join(invalid), key)


def _canonical_factors(lora_a: Tensor, lora_b: Tensor) -> tuple[Tensor, Tensor, Tensor]:
    lora_a, lora_b = lora_a.detach().double(), lora_b.detach().double()
    q_a, r_a = torch.linalg.qr(lora_a.T, mode="reduced")
    q_b, r_b = torch.linalg.qr(lora_b, mode="reduced")
    u, singular_values, vh = torch.linalg.svd(r_b @ r_a.T, full_matrices=False)
    square_roots = singular_values.clamp_min(0).sqrt()
    candidate_b = (q_b @ u) * square_roots.unsqueeze(0)
    candidate_a = square_roots.unsqueeze(1) * (vh @ q_a.T)
    return candidate_a.float(), candidate_b.float(), singular_values.float()


def _canonicalize(
    calibration: ARACalibration, lora_a: Tensor, lora_b: Tensor
) -> tuple[float, float]:
    inputs = torch.cat((calibration.good_inputs, calibration.bad_inputs))
    before = (inputs @ lora_a.detach().T) @ lora_b.detach().T
    candidate_a, candidate_b, singular_values = _canonical_factors(lora_a, lora_b)
    _ensure_finite(
        "canonicalization",
        calibration.key,
        lora_a=candidate_a,
        lora_b=candidate_b,
        singular_values=singular_values,
    )
    after = (inputs @ candidate_a.T) @ candidate_b.T
    difference = after - before
    error = float(difference.abs().max().item())
    relative_error = (
        torch.linalg.vector_norm(difference)
        .div(torch.linalg.vector_norm(before).clamp_min(MIN_SCALE))
        .item()
    )
    maximum_relative_error = float(
        difference.abs().max().div(before.abs().max().clamp_min(MIN_SCALE)).item()
    )
    if relative_error > 1e-5 or maximum_relative_error > 1e-4:
        raise ARAOptimizationError(
            "canonicalization",
            f"deployment output changed by {error:.3e}; relative errors "
            f"norm={relative_error:.3e}, max={maximum_relative_error:.3e}",
            calibration.key,
        )
    lora_a.detach().copy_(candidate_a)
    lora_b.detach().copy_(candidate_b)
    maximum = float(singular_values.max().item()) if len(singular_values) else 0.0
    return error, maximum


def _optimization_stats(
    metrics: tuple[float, float, int, float],
    calibration: ARACalibration,
    factors: tuple[Tensor, Tensor],
    canonical: tuple[float, float],
) -> ARAOptimizationStats:
    initial, final, calls, elapsed = metrics
    lora_a, lora_b = factors
    error, maximum = canonical
    return ARAOptimizationStats(
        initial_loss=initial,
        final_loss=final,
        closure_calls=calls,
        good_samples=len(calibration.good_inputs),
        bad_samples=len(calibration.bad_inputs),
        lora_a_norm=float(torch.linalg.vector_norm(lora_a).item()),
        lora_b_norm=float(torch.linalg.vector_norm(lora_b).item()),
        max_singular_value=maximum,
        canonicalization_max_output_error=error,
        elapsed_seconds=elapsed,
    )


def _run_optimizer(
    calibration: ARACalibration,
    lora_a: Tensor,
    lora_b: Tensor,
    parameters: ARAComponentParameters,
    config: ARAOptimizerConfig,
) -> tuple[float, float, int]:
    closure_calls = 0
    optimizer = _make_optimizer((lora_a, lora_b), config)
    try:
        initial = calculate_ara_loss(
            calibration, lora_a, lora_b, parameters, temperature=config.temperature
        ).total
        _ensure_finite("initial-loss", calibration.key, loss=initial)
        lora_a.requires_grad_(True)
        lora_b.requires_grad_(True)

        def closure() -> Tensor:
            nonlocal closure_calls
            closure_calls += 1
            optimizer.zero_grad(set_to_none=True)
            loss = calculate_ara_loss(
                calibration,
                lora_a,
                lora_b,
                parameters,
                temperature=config.temperature,
            ).total
            _ensure_finite("optimizer", calibration.key, loss=loss)
            loss.backward()
            return loss

        with torch.enable_grad():
            optimizer.step(closure)
        final = calculate_ara_loss(
            calibration, lora_a, lora_b, parameters, temperature=config.temperature
        ).total
        _ensure_finite(
            "final-loss", calibration.key, loss=final, lora_a=lora_a, lora_b=lora_b
        )
        if float(final.detach()) > float(initial.detach()) + 1e-6:
            raise ARAOptimizationError(
                "final-loss", "objective did not decrease", calibration.key
            )
        return float(initial.detach()), float(final.detach()), closure_calls
    finally:
        optimizer.zero_grad(set_to_none=True)


def optimize_ara_module(
    calibration: ARACalibration,
    lora_a: Tensor,
    lora_b: Tensor,
    parameters: ARAComponentParameters,
    optimizer_config: ARAOptimizerConfig,
) -> ARAOptimizationStats:
    """Optimize one module transactionally with L-BFGS and canonicalize it."""

    started_at = time.perf_counter()
    if parameters.strength == 0:
        metrics = 0.0, 0.0, 0, time.perf_counter() - started_at
        return _optimization_stats(metrics, calibration, (lora_a, lora_b), (0.0, 0.0))
    _validate_calibration(calibration, (lora_a, lora_b))
    originals = lora_a.detach().clone(), lora_b.detach().clone()
    original_flags = lora_a.requires_grad, lora_b.requires_grad
    local = _calibration_to_device(calibration, lora_a.device)
    reference = _gram_balance(lora_a, lora_b).detach().clamp_min(MIN_SCALE)
    local = replace(local, balance_reference=reference)
    try:
        initial, final, calls = _run_optimizer(
            local, lora_a, lora_b, parameters, optimizer_config
        )
        canonical = _canonicalize(local, lora_a, lora_b)
        deployed = calculate_ara_loss(
            local, lora_a, lora_b, parameters, temperature=optimizer_config.temperature
        ).total
        _ensure_finite("deployed-loss", calibration.key, loss=deployed)
        final = float(deployed.detach())
        if final > initial + 1e-6:
            raise ARAOptimizationError(
                "deployed-loss", "objective did not decrease", calibration.key
            )
        metrics = initial, final, calls, time.perf_counter() - started_at
        return _optimization_stats(metrics, local, (lora_a, lora_b), canonical)
    except BaseException:
        with torch.no_grad():
            lora_a.copy_(originals[0])
            lora_b.copy_(originals[1])
        raise
    finally:
        lora_a.requires_grad_(original_flags[0])
        lora_b.requires_grad_(original_flags[1])
        lora_a.grad = None
        lora_b.grad = None


def _make_optimizer(
    factors: tuple[Tensor, Tensor], config: ARAOptimizerConfig
) -> torch.optim.LBFGS:
    return torch.optim.LBFGS(
        factors,
        lr=1.0,
        max_iter=config.max_iter,
        max_eval=config.max_eval,
        history_size=config.history_size,
        line_search_fn="strong_wolfe",
        tolerance_grad=1e-7,
        tolerance_change=1e-9,
    )
