# SPDX-License-Identifier: AGPL-3.0-or-later
"""Versioned configuration contracts for CARA experiments."""

from __future__ import annotations

import math
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, PositiveInt, model_validator

ARA_PARAMETER_NAMES = (
    "layer_start",
    "layer_span",
    "attn_strength",
    "mlp_strength",
    "push_weight",
    "margin",
    "attn_deployment_gain",
    "mlp_deployment_gain",
)

RUN_CONTROL_FIELDS = frozenset(
    {
        "checkpoint_action",
        "collect_reproducibles",
        "evaluate_model",
        "export_strategy",
        "ignore_mismatches",
        "model_action",
        "n_additional_trials",
        "n_trials",
        "print_debug_information",
        "recover_orphaned_trials",
        "reproduce",
        "save_directory",
        "study_checkpoint_dir",
        "trial_index",
        "upload_repo_id",
        "upload_repo_private",
        "upload_reproducibility_information",
    }
)


class DatasetSpecification(BaseModel):
    """Pinned prompt dataset and deterministic split description."""

    dataset: str = Field(
        description="Hugging Face dataset ID, or path to dataset on disk."
    )
    commit: str | None = Field(
        default=None,
        description="Hugging Face commit hash of the dataset.",
    )
    split: str | None = Field(
        default=None,
        description="Dataset split or absolute slice.",
    )
    column: str | None = Field(
        default=None,
        description="Column containing prompts.",
    )
    prefix: str = Field(default="", description="Text prepended to each prompt.")
    suffix: str = Field(default="", description="Text appended to each prompt.")
    system_prompt: str | None = Field(
        default=None,
        description="Dataset-specific system prompt.",
    )
    residual_plot_label: str | None = Field(
        default=None,
        description="Plot label for this dataset.",
        exclude=True,
    )
    residual_plot_color: str | None = Field(
        default=None,
        description="Plot color for this dataset.",
        exclude=True,
    )


class ARASearchSpace(BaseModel):
    """Eight-dimensional trajectory-v2 search ranges."""

    layer_start: tuple[float, float] = (0.15, 0.50)
    layer_span: tuple[float, float] = (0.35, 0.75)
    attn_strength: tuple[float, float] = (0.05, 4.0)
    mlp_strength: tuple[float, float] = (0.0001, 2.0)
    push_weight: tuple[float, float] = (0.5, 6.0)
    margin: tuple[float, float] = (2.0, 16.0)
    attn_deployment_gain: tuple[float, float] = (0.75, 4.0)
    mlp_deployment_gain: tuple[float, float] = (0.50, 3.0)

    @model_validator(mode="after")
    def validate_ranges(self) -> "ARASearchSpace":
        """Reject non-finite, reversed, or non-positive log ranges."""
        for name in ARA_PARAMETER_NAMES:
            lower, upper = getattr(self, name)
            if not math.isfinite(lower) or not math.isfinite(upper):
                raise ValueError(f"{name} search range must be finite")
            if lower >= upper:
                raise ValueError(f"{name} search range must increase")
            if name not in {"layer_start", "layer_span"} and lower <= 0:
                raise ValueError(f"{name} log range must be positive")
        if not 0 <= self.layer_start[0] < self.layer_start[1] < 1:
            raise ValueError("layer_start range must remain inside [0, 1)")
        if not 0 < self.layer_span[0] < self.layer_span[1] <= 1:
            raise ValueError("layer_span range must remain inside (0, 1]")
        return self


class ARASeedTrial(BaseModel):
    """One fully specified, pre-registered trajectory anchor."""

    layer_start: float
    layer_span: float
    attn_strength: float
    mlp_strength: float
    push_weight: float
    margin: float
    attn_deployment_gain: float
    mlp_deployment_gain: float

    model_config = {"extra": "forbid"}


class ARARuntimeTarget(BaseModel):
    """Expected inventory and matrix shape for one target name pattern."""

    name_pattern: str = Field(min_length=1)
    count: PositiveInt
    out_features: PositiveInt
    in_features: PositiveInt


class ARARuntimeGuard(BaseModel):
    """Explicit module, device, and resource contract for trajectory CARA."""

    expected_target_total: PositiveInt
    required_target_devices: list[str] = Field(min_length=1)
    min_free_cuda_gib_after_load: float = Field(default=1.5, ge=0)
    max_capture_cpu_gib: float = Field(default=32.0, gt=0)
    max_cuda_allocated_gib: float = Field(default=80.0, gt=0)
    targets: list[ARARuntimeTarget] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_inventory(self) -> "ARARuntimeGuard":
        """Ensure target patterns and counts form an unambiguous inventory."""
        patterns = [target.name_pattern for target in self.targets]
        if len(patterns) != len(set(patterns)):
            raise ValueError("runtime target name patterns must be unique")
        if sum(target.count for target in self.targets) != self.expected_target_total:
            raise ValueError("runtime target counts do not match expected_target_total")
        if len(self.required_target_devices) != len(set(self.required_target_devices)):
            raise ValueError("required_target_devices must be unique")
        limits = (
            self.min_free_cuda_gib_after_load,
            self.max_capture_cpu_gib,
            self.max_cuda_allocated_gib,
        )
        if not all(math.isfinite(value) for value in limits):
            raise ValueError("runtime resource limits must be finite")
        return self


class AcceptanceGate(BaseModel):
    """Deterministic study-health, validation, and audit criteria."""

    keyword_score: str = Field(description="Keyword scorer display name.")
    keyword_max: float = Field(ge=0, le=1)
    keyword_drop_min: float = Field(ge=0, le=1)
    kl_score: str = Field(description="KL scorer display name.")
    kl_max: float = Field(ge=0)
    expected_samples: PositiveInt = 100
    required_trials: PositiveInt = 120
    min_complete_trials: PositiveInt = 110
    max_runtime_failure_rate: float = Field(default=0.05, ge=0, le=1)
    required_chat_template_kwargs: dict[str, Any] = Field(
        default_factory=lambda: {"enable_thinking": False}
    )
    selection: Literal["lexicographic"] = "lexicographic"
    keyword_audit_prompts: DatasetSpecification
    kl_audit_prompts: DatasetSpecification
    report_path: str = Field(default="acceptance.json", exclude=True)

    @model_validator(mode="after")
    def validate_gate(self) -> "AcceptanceGate":
        """Validate names, pinned audit data, and study thresholds."""
        if not self.keyword_score.strip() or not self.kl_score.strip():
            raise ValueError("acceptance score names must not be empty")
        if self.keyword_score == self.kl_score:
            raise ValueError("acceptance score names must be unique")
        if not self.keyword_audit_prompts.commit or not self.kl_audit_prompts.commit:
            raise ValueError("acceptance audit datasets must pin a commit")
        if self.min_complete_trials > self.required_trials:
            raise ValueError("min_complete_trials cannot exceed required_trials")
        if not math.isfinite(self.kl_max):
            raise ValueError("kl_max must be finite")
        return self


def dataset_specs_overlap(
    left: DatasetSpecification,
    right: DatasetSpecification,
) -> bool:
    """Return whether two simple pinned dataset slices may share rows."""
    if left.dataset != right.dataset:
        return False
    if not left.commit or not right.commit:
        return True
    if left.commit != right.commit:
        return False
    left_split, right_split = left.split or "", right.split or ""
    if not left_split or not right_split:
        return True
    pattern = re.compile(r"^([^[]+)(?:\[(\d*):(\d*)\])?$")
    matches = [pattern.match(value) for value in (left_split, right_split)]
    if not all(matches):
        return True
    parsed: list[tuple[str, float, float]] = []
    for match in matches:
        assert match is not None
        parsed.append(
            (
                match.group(1),
                int(match.group(2) or 0),
                int(match.group(3)) if match.group(3) else math.inf,
            )
        )
    return parsed[0][0] == parsed[1][0] and max(parsed[0][1], parsed[1][1]) < min(
        parsed[0][2], parsed[1][2]
    )


def acceptance_scorer_prompts(
    scorers: list[Any],
    extras: dict[str, Any] | None,
    gate: AcceptanceGate,
) -> list[DatasetSpecification]:
    """Resolve explicitly configured validation data for both gate scorers."""
    tables = (extras or {}).get("scorer", {})
    requirements = (
        (
            gate.keyword_score,
            "heretic.scorers.keyword_rate.KeywordRate",
            "KeywordRate",
            "Keywords",
        ),
        (
            gate.kl_score,
            "heretic.scorers.kl_divergence.KLDivergence",
            "KLDivergence",
            "KL divergence",
        ),
    )
    resolved = []
    for score_name, plugin, class_name, display_name in requirements:
        matches = [
            item
            for item in scorers
            if item.plugin == plugin
            and score_name
            == (
                f"{display_name} - {item.instance_name}"
                if item.instance_name
                else display_name
            )
        ]
        if len(matches) != 1 or not isinstance(tables, dict):
            raise ValueError(
                f"acceptance target scorer is not configured: {score_name}"
            )
        item = matches[0]
        base = tables.get(class_name, {})
        instance = tables.get(f"{class_name}_{item.instance_name}", {})
        if not isinstance(base, dict) or not isinstance(instance, dict):
            raise ValueError("acceptance scorer settings must be tables")
        raw = instance.get("prompts", base.get("prompts"))
        if not isinstance(raw, dict):
            raise ValueError(
                f"acceptance scorer must explicitly configure prompts: {score_name}"
            )
        resolved.append(DatasetSpecification.model_validate(raw))
    return resolved


def configured_dataset_specs(
    extras: dict[str, Any] | None,
) -> list[DatasetSpecification]:
    """Collect nested dataset specifications from plugin settings."""
    candidates = []
    tables = (extras or {}).get("scorer", {})
    if not isinstance(tables, dict):
        raise ValueError("scorer settings must be tables")
    stack = list(tables.values())
    while stack:
        value = stack.pop()
        if isinstance(value, dict) and "dataset" in value:
            candidates.append(DatasetSpecification.model_validate(value))
        elif isinstance(value, dict):
            stack.extend(value.values())
    return candidates


def validate_seed_trials(
    seeds: list[ARASeedTrial],
    search_space: ARASearchSpace,
) -> None:
    """Ensure all anchor coordinates lie inside the configured search space."""
    for index, seed in enumerate(seeds):
        for name in ARA_PARAMETER_NAMES:
            value = getattr(seed, name)
            lower, upper = getattr(search_space, name)
            if not lower <= value <= upper:
                raise ValueError(f"ara_seed_trials[{index}].{name} is out of range")
