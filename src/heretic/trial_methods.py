# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026  Philipp Emanuel Weidmann <pew@worldwidemann.com> + contributors
"""Method-neutral Optuna sampling, application, and acceptance logic."""

import json
import math
import statistics
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast

import bitsandbytes.functional as bnbf
import torch
import torch.nn.functional as F
from optuna import Trial
from optuna.trial import FrozenTrial
from peft.tuners.lora.layer import Linear
from torch import Tensor

from .ara import (
    ARAArtifacts,
    ARAComponentParameters,
    ARAOptimizationStats,
    ARAOptimizationSummary,
    ARAParameters,
    get_lora_factors,
    optimize_ara_module,
    restore_adapter_state,
)
from .ara_search import (
    ARASamplingContext,
    ARATrajectoryParameters,
    SEARCH_SPACE_VERSION as TRAJECTORY_SEARCH_SPACE_VERSION,
    parameter_envelope as trajectory_parameter_envelope,
    parse_parameter_envelope as parse_trajectory_parameter_envelope,
    sample_v2_parameters,
)
from .ara_trajectory import (
    TrajectoryCalibration,
    TrajectoryModuleParameters,
    TrajectoryOptimizationStats,
    TrajectoryOptimizerConfig,
    optimize_trajectory_module,
)
from .acceptance import (
    collect_legacy_failure_records,
    legacy_failure_constraint,
    legacy_trial_failure_record,
    safe_failure_reason as _safe_failure_reason,
    select_legacy_candidate,
    validate_legacy_gate_records,
)
from .artifact_schema import canonical_sha256, manifest_differences
from .config import (
    AbliterationMethod,
    AcceptanceGate,
    RowNormalization,
    Settings,
)
from .model import AbliterationParameters, Model
from .system import empty_cache
from .utils import print

SEARCH_SPACE_VERSION = "cara-search-v1"
STUDY_SCHEMA_VERSION = "cara-study-v1"
ACCEPTANCE_SCHEMA_VERSION = "cara-acceptance-v1"
failure_constraint = legacy_failure_constraint
failure_record = legacy_trial_failure_record
safe_failure_reason = _safe_failure_reason


@dataclass(frozen=True)
class DirectionalParameters:
    """Resolved parameters for the legacy directional method."""

    direction_index: float | None
    components: dict[str, AbliterationParameters]


@dataclass(frozen=True)
class DirectionalArtifacts:
    """Residual directions needed to apply a directional trial."""

    residual_directions: torch.Tensor


@dataclass(frozen=True)
class MethodContext:
    """Stable model/settings context used to sample a method search space."""

    settings: Settings
    layer_count: int
    components: tuple[str, ...]


@dataclass(frozen=True)
class MethodApplicationSummary:
    """Method-neutral result of applying one resolved trial."""

    method: AbliterationMethod
    ara: ARAOptimizationSummary | None = None


@dataclass(frozen=True)
class TrajectoryArtifacts:
    """Captured trajectory bank and adapter transaction state."""

    calibration_bank: Mapping[Any, TrajectoryCalibration]
    adapter_initial_state: Any
    targets: tuple[Any, ...]
    optimizer_config: TrajectoryOptimizerConfig
    trajectory_fingerprint: str
    trajectory_manifest: Mapping[str, Any]


@dataclass(frozen=True)
class AcceptanceReport:
    """Machine-readable evidence envelope for an acceptance decision."""

    status: Literal["passed", "failed"]
    reason: str
    model_fingerprint: str
    study_fingerprint: str
    calibration_fingerprint: str | None = None
    selected_trial_number: int | None = None
    parameters: dict[str, Any] | None = None
    validation_scores: list[dict[str, Any]] | None = None
    audit_scores: list[dict[str, Any]] | None = None
    reload_scores: list[dict[str, Any]] | None = None
    validation_replay_scores: list[list[dict[str, Any]]] | None = None
    parameter_comparison: dict[str, Any] | None = None
    score_drift: dict[str, float] | None = None
    module_counts: dict[str, int] | None = None
    resource_peaks: dict[str, float] | None = None
    environment_versions: dict[str, str] | None = None
    failure_trials: list[dict[str, Any]] | None = None
    artifact_hashes: dict[str, str] | None = None
    schema_version: str = ACCEPTANCE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible report."""

        return asdict(self)


MethodParameters = DirectionalParameters | ARAParameters | ARATrajectoryParameters
MethodArtifacts = DirectionalArtifacts | ARAArtifacts | TrajectoryArtifacts


class AcceptanceGateError(RuntimeError):
    """Raised when no trustworthy trial can be selected by an acceptance gate."""


def _sample_directional(trial: Trial, context: MethodContext) -> DirectionalParameters:
    scope = trial.suggest_categorical("direction_scope", ["global", "per layer"])
    last_layer = context.layer_count - 1
    direction_index = trial.suggest_float(
        "direction_index", 0.4 * last_layer, 0.9 * last_layer
    )
    if scope == "per layer":
        direction_index = None
    components = {
        component: _sample_directional_component(trial, component, last_layer)
        for component in context.components
    }
    return DirectionalParameters(direction_index, components)


def _sample_directional_component(
    trial: Trial, component: str, last_layer: int
) -> AbliterationParameters:
    lower = -0.25 if component == "mlp.down_proj" else 0.8
    maximum = max(0.0, trial.suggest_float(f"{component}.max_weight", lower, 1.5))
    position = trial.suggest_float(
        f"{component}.max_weight_position", 0.6 * last_layer, 1.0 * last_layer
    )
    minimum_fraction = trial.suggest_float(f"{component}.min_weight", 0.0, 1.0)
    distance = trial.suggest_float(
        f"{component}.min_weight_distance", 1.0, max(0.6 * last_layer, 1.0)
    )
    return AbliterationParameters(
        max_weight=maximum,
        max_weight_position=position,
        min_weight=minimum_fraction * maximum,
        min_weight_distance=distance,
    )


def _component_slug(component: str) -> str:
    return component.replace(".", "_").replace("-", "_")


def _sample_ara(trial: Trial, context: MethodContext) -> ARAParameters:
    start_fraction = trial.suggest_float("ara.layer_start_fraction", 0.25, 0.65)
    span_fraction = trial.suggest_float("ara.layer_span_fraction", 0.10, 0.55)
    attention = trial.suggest_float("ara.attn_strength", 1e-3, 1.0, log=True)
    mlp_raw = trial.suggest_float("ara.mlp_strength_raw", -0.10, 0.50)
    push_weight = trial.suggest_float("ara.push_weight", 0.0, 2.0)
    margin = trial.suggest_float("ara.margin", 0.25, 4.0, log=True)
    strengths = {"attn.o_proj": attention, "mlp.down_proj": max(0.0, mlp_raw)}
    for component in context.components:
        if component not in strengths:
            strengths[component] = trial.suggest_float(
                f"ara.{_component_slug(component)}.strength", 1e-3, 1.0, log=True
            )
    start = math.floor(start_fraction * context.layer_count)
    span = math.ceil(span_fraction * context.layer_count)
    end = min(context.layer_count, max(start + 1, start + span))
    parameters = {
        component: ARAComponentParameters(strengths[component], push_weight, margin)
        for component in context.components
    }
    return ARAParameters(start, end, parameters)


def sample_method_parameters(trial: Trial, context: MethodContext) -> MethodParameters:
    """Sample all dimensions for the configured method without conditional ranges."""

    if context.settings.abliteration_method == AbliterationMethod.ARA:
        if context.settings.ara_objective_version == "trajectory-v2":
            parameters = sample_v2_parameters(
                trial,
                ARASamplingContext(context.layer_count),
                context.settings.ara_search_space,
            )
        else:
            parameters = _sample_ara(trial, context)
    else:
        parameters = _sample_directional(trial, context)
    store_method_parameters(trial, parameters)
    return parameters


def _ara_payload(parameters: ARAParameters) -> dict[str, Any]:
    return {
        "start_layer_index": parameters.start_layer_index,
        "end_layer_index": parameters.end_layer_index,
        "components": {
            name: asdict(component) for name, component in parameters.components.items()
        },
    }


def parameter_envelope(parameters: MethodParameters) -> dict[str, Any]:
    """Serialize resolved method parameters using the reproduce-v4 envelope."""

    if isinstance(parameters, ARAParameters):
        return {"method": "ara", "payload": _ara_payload(parameters)}
    if isinstance(parameters, ARATrajectoryParameters):
        return trajectory_parameter_envelope(parameters)
    return {
        "method": "directional",
        "payload": {
            "direction_index": parameters.direction_index,
            "abliteration_parameters": {
                name: asdict(component)
                for name, component in parameters.components.items()
            },
        },
    }


def store_method_parameters(trial: Trial, parameters: MethodParameters) -> None:
    """Store resolved parameters while preserving directional legacy attributes."""

    envelope = parameter_envelope(parameters)
    trial.set_user_attr("method", envelope["method"])
    if isinstance(parameters, ARAParameters):
        trial.set_user_attr("search_space_version", SEARCH_SPACE_VERSION)
        trial.set_user_attr("ara_parameters", envelope["payload"])
        return
    if isinstance(parameters, ARATrajectoryParameters):
        trial.set_user_attr("objective_version", "trajectory-v2")
        trial.set_user_attr(
            "search_space_version",
            TRAJECTORY_SEARCH_SPACE_VERSION,
        )
        trial.set_user_attr("ara_parameters", envelope)
        return
    trial.set_user_attr("direction_index", parameters.direction_index)
    trial.set_user_attr(
        "parameters",
        {name: asdict(value) for name, value in parameters.components.items()},
    )


def parse_parameter_envelope(envelope: Mapping[str, Any]) -> MethodParameters:
    """Validate and deserialize a reproduce-v4 method envelope."""

    if envelope.get("objective_version") == "trajectory-v2":
        return parse_trajectory_parameter_envelope(envelope)
    method = envelope.get("method")
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("method parameter payload must be an object")
    if method == "directional":
        raw_components = cast(
            Mapping[str, Mapping[str, Any]], payload.get("abliteration_parameters")
        )
        if not isinstance(raw_components, Mapping):
            raise ValueError("directional payload has no abliteration_parameters")
        return DirectionalParameters(
            direction_index=cast(float | None, payload.get("direction_index")),
            components={
                name: AbliterationParameters(**value)
                for name, value in raw_components.items()
            },
        )
    if method == "ara":
        raw_components = cast(
            Mapping[str, Mapping[str, Any]], payload.get("components")
        )
        if not isinstance(raw_components, Mapping):
            raise ValueError("ARA payload has no components")
        return ARAParameters(
            start_layer_index=int(payload["start_layer_index"]),
            end_layer_index=int(payload["end_layer_index"]),
            components={
                name: ARAComponentParameters(**value)
                for name, value in raw_components.items()
            },
        )
    raise ValueError(f"unsupported abliteration method: {method}")


def parameters_from_trial(trial: Trial | FrozenTrial) -> MethodParameters:
    """Deserialize resolved parameters from a current or legacy Optuna trial."""

    method = trial.user_attrs.get("method", "directional")
    if method == "ara":
        raw_envelope = trial.user_attrs["ara_parameters"]
        if trial.user_attrs.get("objective_version") == "trajectory-v2":
            return parse_parameter_envelope(raw_envelope)
        return parse_parameter_envelope({"method": "ara", "payload": raw_envelope})
    return parse_parameter_envelope(
        {
            "method": "directional",
            "payload": {
                "direction_index": trial.user_attrs["direction_index"],
                "abliteration_parameters": trial.user_attrs["parameters"],
            },
        }
    )


def normalize_reproduction_parameters(information: Mapping[str, Any]) -> dict[str, Any]:
    """Migrate reproduce v3 parameters or validate a v4 envelope."""

    version = str(information.get("version"))
    raw = information.get("parameters")
    if not isinstance(raw, Mapping):
        raise ValueError("reproduction information has no parameter object")
    if information.get("schema") == "cara-reproduce-v2":
        from .artifact_schema import parse_reproduce

        parse_reproduce(information)
        envelope = dict(raw)
    elif version == "3":
        envelope = {
            "method": "directional",
            "payload": {
                "direction_index": raw.get("direction_index"),
                "abliteration_parameters": raw.get("abliteration_parameters"),
            },
        }
    elif version == "4":
        envelope = dict(raw)
    else:
        raise ValueError(f"unsupported reproduce.json version: {version}")
    parse_parameter_envelope(envelope)
    return envelope


def _apply_ara(
    model: Model, parameters: ARAParameters, artifacts: ARAArtifacts
) -> ARAOptimizationSummary:
    restore_adapter_state(artifacts.targets, artifacts.adapter_initial_state)
    stats: list[ARAOptimizationStats] = []
    skipped = 0
    started_at = time.perf_counter()
    try:
        for target in artifacts.targets:
            if (
                not parameters.start_layer_index
                <= target.key.layer_index
                < parameters.end_layer_index
            ):
                skipped += 1
                continue
            component = parameters.components[target.key.component]
            if component.strength == 0:
                skipped += 1
                continue
            calibration = artifacts.calibration_bank[target.key]
            lora_a, lora_b = get_lora_factors(target)
            module_stats = optimize_ara_module(
                calibration, lora_a, lora_b, component, artifacts.optimizer_config
            )
            stats.append(module_stats)
            if model.settings.print_debug_information:
                print(
                    f"  * {target.full_name}: {module_stats.initial_loss:.6g} -> "
                    f"{module_stats.final_loss:.6g} ({module_stats.closure_calls} closures)"
                )
    except BaseException:
        restore_adapter_state(artifacts.targets, artifacts.adapter_initial_state)
        raise
    return _summarize_ara(stats, skipped, time.perf_counter() - started_at)


def _trajectory_component(
    target: Any,
    parameters: ARATrajectoryParameters,
) -> TrajectoryModuleParameters:
    is_attention = "attn" in target.key.component
    return TrajectoryModuleParameters(
        strength=(
            parameters.attn_strength if is_attention else parameters.mlp_strength
        ),
        push_weight=parameters.push_weight,
        margin=parameters.margin,
        deployment_gain=(
            parameters.attn_deployment_gain
            if is_attention
            else parameters.mlp_deployment_gain
        ),
    )


def _apply_trajectory(
    model: Model,
    parameters: ARATrajectoryParameters,
    artifacts: TrajectoryArtifacts,
) -> ARAOptimizationSummary:
    restore_adapter_state(artifacts.targets, artifacts.adapter_initial_state)
    stats: list[TrajectoryOptimizationStats] = []
    skipped = 0
    started_at = time.perf_counter()
    try:
        for target in artifacts.targets:
            if not (
                parameters.start_layer_index
                <= target.key.layer_index
                < parameters.end_layer_index
            ):
                skipped += 1
                continue
            lora_a, lora_b = get_lora_factors(target)
            stats.append(
                optimize_trajectory_module(
                    artifacts.calibration_bank[target.key],
                    lora_a,
                    lora_b,
                    _trajectory_component(target, parameters),
                    artifacts.optimizer_config,
                )
            )
    except BaseException:
        restore_adapter_state(artifacts.targets, artifacts.adapter_initial_state)
        raise
    losses = [item.loss for item in stats]
    return ARAOptimizationSummary(
        processed_modules=len(stats),
        skipped_modules=skipped,
        failed_modules=0,
        median_initial_loss=0.0,
        median_final_loss=statistics.median(losses) if losses else 0.0,
        max_final_loss=max(losses, default=0.0),
        elapsed_seconds=time.perf_counter() - started_at,
    )


def _direction_at(
    residual_directions: Tensor, direction_index: float | None, layer_index: int
) -> Tensor:
    if direction_index is None:
        return residual_directions[layer_index + 1]
    fraction, index = math.modf(direction_index + 1)
    return F.normalize(
        residual_directions[int(index)].lerp(
            residual_directions[int(index) + 1], fraction
        ),
        p=2,
        dim=0,
    )


def _directional_weight(
    parameters: AbliterationParameters, layer_index: int
) -> float | None:
    distance = abs(layer_index - parameters.max_weight_position)
    if distance > parameters.min_weight_distance:
        return None
    return parameters.max_weight + (distance / parameters.min_weight_distance) * (
        parameters.min_weight - parameters.max_weight
    )


def _base_weight(module: Linear) -> Tensor:
    base_weight = cast(Tensor, module.base_layer.weight)
    quantization = getattr(base_weight, "quant_state", None)
    if quantization is None:
        return base_weight.to(torch.float32).view(base_weight.shape[0], -1)
    return (
        bnbf.dequantize_4bit(base_weight.data, quantization)
        .to(torch.float32)
        .view(base_weight.shape[0], -1)
    )


def _full_normalized_factors(
    matrix: Tensor,
    normalized: Tensor,
    lora_a: Tensor,
    lora_b: Tensor,
    settings: tuple[Tensor, int, int],
) -> tuple[Tensor, Tensor]:
    row_norms, rank, seed = settings
    delta = F.normalize(normalized + lora_b @ lora_a, p=2, dim=1)
    delta = delta * row_norms - matrix
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)  # ty:ignore[invalid-argument-type]
    u, singular, vh = torch.svd_lowrank(delta, q=2 * rank + 4, niter=6)
    roots = torch.sqrt(singular[:rank])
    return torch.diag(roots) @ vh[:, :rank].T, u[:, :rank] @ torch.diag(roots)


def _directional_factors(
    model: Model, module: Linear, direction: Tensor, weight: float
) -> tuple[Tensor, Tensor]:
    matrix = _base_weight(module)
    row_norms = torch.linalg.vector_norm(matrix, dim=1, keepdim=True)
    working = matrix
    if model.settings.row_normalization != RowNormalization.NONE:
        working = F.normalize(matrix, p=2, dim=1)
    lora_a = (direction @ working).view(1, -1)
    lora_b = (-weight * direction).view(-1, 1)
    if model.settings.row_normalization == RowNormalization.PRE:
        lora_b = row_norms * lora_b
    elif model.settings.row_normalization == RowNormalization.FULL:
        lora_a, lora_b = _full_normalized_factors(
            matrix,
            working,
            lora_a,
            lora_b,
            (row_norms, model.peft_config.r, cast(int, model.settings.seed)),
        )
    return lora_a, lora_b


def _apply_directional(
    model: Model, parameters: DirectionalParameters, artifacts: DirectionalArtifacts
) -> None:
    for layer_index in range(len(model.get_layers())):
        direction = _direction_at(
            artifacts.residual_directions, parameters.direction_index, layer_index
        )
        for component, modules in model.get_layer_modules(layer_index).items():
            weight = _directional_weight(parameters.components[component], layer_index)
            if not weight:
                continue
            for candidate in modules:
                module = cast(Linear, candidate)
                local_direction = direction.to(module.weight.device)
                lora_a, lora_b = _directional_factors(
                    model, module, local_direction, weight
                )
                target_a = cast(Tensor, module.lora_A["default"].weight)
                target_b = cast(Tensor, module.lora_B["default"].weight)
                target_a.data = lora_a.to(target_a.dtype)
                target_b.data = lora_b.to(target_b.dtype)


def _summarize_ara(
    stats: list[ARAOptimizationStats], skipped: int, elapsed: float
) -> ARAOptimizationSummary:
    initial = [item.initial_loss for item in stats]
    final = [item.final_loss for item in stats]
    return ARAOptimizationSummary(
        processed_modules=len(stats),
        skipped_modules=skipped,
        failed_modules=0,
        median_initial_loss=statistics.median(initial) if initial else 0.0,
        median_final_loss=statistics.median(final) if final else 0.0,
        max_final_loss=max(final, default=0.0),
        elapsed_seconds=elapsed,
    )


def apply_trial(
    model: Model,
    parameters: MethodParameters,
    artifacts: MethodArtifacts,
) -> MethodApplicationSummary:
    """Reset and apply either method using only resolved immutable parameters."""

    model.reset_model()
    if isinstance(parameters, DirectionalParameters):
        if not isinstance(artifacts, DirectionalArtifacts):
            raise TypeError("directional parameters require directional artifacts")
        _apply_directional(model, parameters, artifacts)
        return MethodApplicationSummary(AbliterationMethod.DIRECTIONAL)
    if isinstance(parameters, ARATrajectoryParameters):
        if not isinstance(artifacts, TrajectoryArtifacts):
            raise TypeError("trajectory parameters require trajectory artifacts")
        return MethodApplicationSummary(
            AbliterationMethod.ARA,
            _apply_trajectory(model, parameters, artifacts),
        )
    if not isinstance(artifacts, ARAArtifacts):
        raise TypeError("ARA parameters require ARA artifacts")
    return MethodApplicationSummary(
        AbliterationMethod.ARA, _apply_ara(model, parameters, artifacts)
    )


def cleanup_trial(model: Model, artifacts: MethodArtifacts) -> None:
    """Enforce the clean-adapter postcondition after every trial exit path."""

    if isinstance(artifacts, (ARAArtifacts, TrajectoryArtifacts)):
        restore_adapter_state(artifacts.targets, artifacts.adapter_initial_state)
    else:
        model.reset_model()
    empty_cache()


_STUDY_FIELDS = frozenset(
    """abliteration_method acceptance_gate ara_calibration_size
    ara_capture_batch_size ara_lbfgs_history_size ara_lbfgs_max_iter ara_lora_rank
    ara_max_good_delta_rms ara_max_singular_value ara_objective_version
    ara_runtime_guard ara_search_space ara_seed_trials ara_softmin_temperature
    ara_trajectory_decay ara_trajectory_tokens bad_prompts chat_template_kwargs
    full_normalization_lora_rank generation_kwargs good_prompts model model_commit
    max_response_length orthogonalize_direction quantization response_prefix
    row_normalization scorers seed system_prompt target_components
    winsorization_quantile""".split()
)


def build_study_manifest(settings: Settings) -> dict[str, Any]:
    """Build the immutable semantic manifest used to guard study resume."""

    dumped = settings.model_dump(mode="json")
    manifest = {name: dumped[name] for name in sorted(_STUDY_FIELDS)}
    scorer_tables = dumped.get("scorer")
    if scorer_tables is not None:
        manifest["scorer_settings"] = scorer_tables
    manifest.update(
        {
            "schema_version": (
                "cara-study-v2"
                if settings.ara_objective_version == "trajectory-v2"
                else STUDY_SCHEMA_VERSION
            ),
            "search_space_version": (
                TRAJECTORY_SEARCH_SPACE_VERSION
                if settings.ara_objective_version == "trajectory-v2"
                else SEARCH_SPACE_VERSION
            ),
            "optimizer_schema": (
                "cara-trajectory-lbfgs-v2"
                if settings.ara_objective_version == "trajectory-v2"
                else "cara-lbfgs-v1"
            ),
            "calibration_protocol": (
                "cara-teacher-forced-trajectory-v2"
                if settings.ara_objective_version == "trajectory-v2"
                else "cara-capture-v1"
            ),
        }
    )
    return manifest


def canonical_fingerprint(value: Mapping[str, Any]) -> str:
    return canonical_sha256(value)


def build_study_fingerprint(settings: Settings) -> str:
    return canonical_fingerprint(build_study_manifest(settings))


def validate_study_identity(
    stored_fingerprint: str | None,
    stored_manifest: Mapping[str, Any] | None,
    settings: Settings,
) -> tuple[str, dict[str, Any]]:
    """Reject semantic checkpoint drift before appending any trials."""

    current_manifest = build_study_manifest(settings)
    current_fingerprint = canonical_fingerprint(current_manifest)
    if stored_fingerprint is None:
        if settings.abliteration_method == AbliterationMethod.ARA:
            raise ValueError(
                "legacy study has no fingerprint and cannot accept ARA trials"
            )
        return current_fingerprint, current_manifest
    if stored_fingerprint != current_fingerprint:
        differences = manifest_differences(stored_manifest or {}, current_manifest)
        raise ValueError("study fingerprint mismatch: " + ", ".join(differences))
    return current_fingerprint, current_manifest


def validate_gate_records(
    score_records: list[dict[str, Any]], gate: AcceptanceGate
) -> tuple[float, float]:
    """Validate point-v1 scorer records and return keyword/KL values."""
    try:
        return validate_legacy_gate_records(score_records, gate)
    except (ValueError, RuntimeError) as error:
        raise AcceptanceGateError(str(error)) from error


def select_accepted_trial(
    trials: Sequence[FrozenTrial],
    gate: AcceptanceGate,
    study_fingerprint: str,
) -> FrozenTrial:
    try:
        return select_legacy_candidate(trials, gate, study_fingerprint)
    except ValueError as error:
        raise AcceptanceGateError(str(error)) from error


def collect_failure_records(trials: Sequence[FrozenTrial]) -> list[dict[str, Any]]:
    return collect_legacy_failure_records(trials)


def write_acceptance_report(path: str | Path, report: AcceptanceReport) -> None:
    identities = (
        report.model_fingerprint,
        report.study_fingerprint,
        report.calibration_fingerprint,
    )
    if report.status == "passed" and (
        not report.artifact_hashes
        or any(not isinstance(value, str) or not value for value in identities)
    ):
        raise ValueError("a passed acceptance report requires hashes and fingerprints")
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )


def validate_acceptance_binding(
    report: Mapping[str, Any],
    reproduction: Mapping[str, Any],
    artifact_hashes: Mapping[str, str],
) -> None:
    """Reject inconsistent acceptance, reproduce, or adapter identities."""

    if report.get("status") != "passed":
        raise AcceptanceGateError("acceptance report is not passed")
    if reproduction.get("version") != "4":
        raise AcceptanceGateError("accepted artifacts require reproduce v4")
    if reproduction.get("parameters") != report.get("parameters"):
        raise AcceptanceGateError("acceptance parameters do not match reproduce.json")
    for field in (
        "model_fingerprint",
        "study_fingerprint",
        "calibration_fingerprint",
    ):
        if not isinstance(report.get(field), str) or not report[field]:
            raise AcceptanceGateError(f"acceptance report has no {field}")
        if reproduction.get(field) != report.get(field):
            raise AcceptanceGateError(f"{field} mismatch")
    expected = dict(artifact_hashes)
    if report.get("artifact_hashes") != expected:
        raise AcceptanceGateError("acceptance adapter hashes do not match")
    if reproduction.get("hashes") != expected:
        raise AcceptanceGateError("reproduce adapter hashes do not match")


def append_selection_record(
    path: str | Path, trial: FrozenTrial, study_fingerprint: str
) -> None:
    """Append the immutable candidate identity before any audit data is loaded."""

    record = {
        "trial_number": trial.number,
        "study_fingerprint": study_fingerprint,
        "parameters": parameter_envelope(parameters_from_trial(trial)),
    }
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(record, ensure_ascii=False, sort_keys=True)
    if output.is_file():
        previous = output.read_text(encoding="utf-8").splitlines()
        if serialized in previous:
            return
        raise AcceptanceGateError("acceptance candidate lock identity differs")
    with output.open("a", encoding="utf-8") as stream:
        stream.write(serialized + "\n")
