# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026  Philipp Emanuel Weidmann <pew@worldwidemann.com> + contributors

import math
import re
from enum import Enum
from typing import Any, Dict, Literal

from pydantic import (
    BaseModel,
    Field,
    NonNegativeInt,
    PositiveInt,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    CliSettingsSource,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

# Settings with privacy implications must use ``exclude=True``.


class QuantizationMethod(str, Enum):
    NONE = "none"
    BNB_4BIT = "bnb_4bit"


class AbliterationMethod(str, Enum):
    DIRECTIONAL = "directional"
    ARA = "ara"


class RowNormalization(str, Enum):
    NONE = "none"
    PRE = "pre"
    FULL = "full"


class ExportStrategy(str, Enum):
    MERGE = "merge"
    ADAPTER = "adapter"


class DatasetSpecification(BaseModel):
    dataset: str = Field(
        description="Hugging Face dataset ID, or path to dataset on disk."
    )

    commit: str | None = Field(
        default=None,
        description="Hugging Face commit hash of the dataset.",
    )

    split: str | None = Field(
        default=None,
        description="Portion of the dataset to use. Required for datasets, optional for plain text files.",
    )

    column: str | None = Field(
        default=None,
        description="Column in the dataset that contains the prompts. Required for datasets, ignored for plain text files.",
    )

    prefix: str = Field(
        default="",
        description="Text to prepend to each prompt.",
    )

    suffix: str = Field(
        default="",
        description="Text to append to each prompt.",
    )

    system_prompt: str | None = Field(
        default=None,
        description="System prompt to use with the prompts (overrides global system prompt if set).",
    )

    residual_plot_label: str | None = Field(
        default=None,
        description="Label to use for the dataset in plots of residual vectors.",
        exclude=True,
    )

    residual_plot_color: str | None = Field(
        default=None,
        description="Matplotlib color to use for the dataset in plots of residual vectors.",
        exclude=True,
    )


def dataset_specs_overlap(
    left: DatasetSpecification, right: DatasetSpecification
) -> bool:
    """Return whether two simple Hugging Face split slices can share rows."""
    if left.dataset != right.dataset:
        return False
    if not left.commit or not right.commit:
        return True
    if left.commit != right.commit:
        return False
    left_split, right_split = left.split or "", right.split or ""
    if not left_split or not right_split:
        return True
    if left_split.partition("[")[0] != right_split.partition("[")[0]:
        return False
    pattern = re.compile(r"^([^[]+)(?:\[(\d*):(\d*)\])?$")
    matches = [pattern.match(spec.split or "") for spec in (left, right)]
    if not all(matches):
        return True
    parsed = []
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


class AcceptanceGate(BaseModel):
    """Deterministic validation and post-export acceptance criteria."""

    keyword_score: str = Field(description="Keyword scorer display name.")
    keyword_max: float = Field(ge=0, le=1)
    keyword_drop_min: float = Field(ge=0, le=1)
    kl_score: str = Field(description="KL scorer display name.")
    kl_max: float = Field(ge=0, le=1)
    expected_samples: PositiveInt = 100
    selection: Literal["lexicographic"] = "lexicographic"
    keyword_audit_prompts: DatasetSpecification
    kl_audit_prompts: DatasetSpecification
    report_path: str = Field(default="acceptance.json", exclude=True)

    @model_validator(mode="after")
    def validate_score_names(self) -> "AcceptanceGate":
        if not self.keyword_score.strip() or not self.kl_score.strip():
            raise ValueError("acceptance score names must not be empty")
        if self.keyword_score == self.kl_score:
            raise ValueError("acceptance score names must be unique")
        if not self.keyword_audit_prompts.commit or not self.kl_audit_prompts.commit:
            raise ValueError("acceptance audit datasets must pin a commit")
        return self


class ScorerConfig(BaseModel):
    """
    Configuration for a scorer plugin.

    TOML format:
    - { plugin = "<plugin>", optimization = "<optimization>", instance_name = "<optional>" }
    """

    plugin: str = Field(
        description=(
            "Plugin to load. Either a file path with class name "
            "(`path/to/plugin.py:ClassName`) or a fully-qualified import path "
            "(`module.submodule.ClassName`)."
        ),
    )

    optimization: Literal["minimize", "maximize", "none"] = Field(
        description=(
            "Optimization direction for this scorer. "
            '"minimize" / "maximize" to include the scorer as an objective, '
            '"none" to compute the score without optimizing for it.'
        ),
    )

    instance_name: str | None = Field(
        default=None,
        description=(
            "Optional name to distinguish multiple instances of the same plugin class. "
            "Instance-specific settings live under `[scorer.<ClassName>_<instance_name>]`."
        ),
    )

    @field_validator("instance_name")
    @classmethod
    def validate_instance_name(cls, value: str | None) -> str | None:
        if value is None:
            return value

        if not value.strip():
            raise ValueError("cannot be empty or whitespace")

        if "." in value:
            raise ValueError("'.' is not allowed")

        if any(char.isspace() for char in value):
            raise ValueError("whitespace is not allowed")

        return value


def _acceptance_scorer_prompts(
    scorers: list[ScorerConfig], extras: dict[str, Any] | None, gate: AcceptanceGate
) -> list[DatasetSpecification]:
    """Resolve the explicitly configured validation datasets for gate scorers."""
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


def _configured_dataset_specs(
    extras: dict[str, Any] | None,
) -> list[DatasetSpecification]:
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


class BenchmarkSpecification(BaseModel):
    task: str = Field(
        description="Task ID of the benchmark in the Language Model Evaluation Harness."
    )

    name: str = Field(description="Name of the benchmark for presentation purposes.")

    description: str = Field(
        description="Description of the benchmark for presentation purposes."
    )


class Settings(BaseSettings):
    model: str = Field(description="Hugging Face model ID, or path to model on disk.")

    model_commit: str | None = Field(
        default=None,
        description="Hugging Face commit hash of the model.",
    )

    abliteration_method: AbliterationMethod = Field(
        default=AbliterationMethod.DIRECTIONAL,
        description='Abliteration method: "directional" or "ara".',
    )

    evaluate_model: str | None = Field(
        default=None,
        description=(
            "If this model ID or path is set, then instead of abliterating the main model, "
            "evaluate this model relative to the main model."
        ),
        exclude=True,
    )

    collect_reproducibles: str | None = Field(
        default=None,
        description=(
            "If this directory path is set, then instead of abliterating a model, "
            "download all reproduce.json files from public Heretic model repositories "
            "on Hugging Face, and store them in that directory for archival purposes."
        ),
        exclude=True,
    )

    reproduce: str | None = Field(
        default=None,
        description=(
            "If this path or URL to a reproduce.json file is set, load reproduction information "
            "from that file, and attempt to reproduce the abliterated model it originated from."
        ),
        exclude=True,
    )

    dtypes: list[str] = Field(
        default=[
            # In practice, "auto" almost always means bfloat16.
            "auto",
            # If that doesn't work (e.g. on pre-Ampere hardware), fall back to float16.
            "float16",
            # If "auto" resolves to float32, and that fails because it is too large,
            # and float16 fails due to range issues, try bfloat16.
            "bfloat16",
            # If neither of those work, fall back to float32 (which will of course fail
            # if that was the dtype "auto" resolved to).
            "float32",
        ],
        description=(
            "List of PyTorch dtypes to try when loading model tensors. "
            "If loading with a dtype fails, the next dtype in the list will be tried."
        ),
    )

    quantization: QuantizationMethod = Field(
        default=QuantizationMethod.NONE,
        description=(
            "Quantization method to use when loading the model. Options: "
            '"none" (no quantization), '
            '"bnb_4bit" (4-bit quantization using bitsandbytes).'
        ),
    )

    device_map: str | Dict[str, int | str] = Field(
        default="auto",
        description="Device map to pass to Accelerate when loading the model.",
    )

    max_memory: Dict[str, str] | None = Field(
        default=None,
        description='Maximum memory to allocate per device (e.g., { "0" = "20GB", "cpu" = "64GB" }).',
    )

    offload_outputs_to_cpu: bool = Field(
        default=True,
        description=(
            "Whether to move intermediate analysis tensors (such as residuals and logprobs) "
            "to CPU memory as soon as possible to reduce peak VRAM usage. "
            "This lowers peak VRAM usage during residual analysis and evaluation, "
            "but may slightly reduce performance due to host/device transfers."
        ),
    )

    batch_size: NonNegativeInt = Field(
        default=0,  # auto
        description="Number of input sequences to process in parallel (0 = auto).",
    )

    max_batch_size: PositiveInt = Field(
        default=128,
        description="Maximum batch size to try when automatically determining the optimal batch size.",
        # When storing a settings object, the batch size is already fixed,
        # either determined by the automatic mechanism or by explicit user choice.
        exclude=True,
    )

    max_response_length: PositiveInt = Field(
        default=100,
        description="Maximum number of tokens to generate for each response.",
    )

    chat_template_kwargs: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional keyword arguments passed to the tokenizer chat template.",
    )

    generation_kwargs: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional keyword arguments passed to model.generate().",
    )

    response_prefix: str | None = Field(
        default=None,
        description=(
            "Common prefix to assume for all responses, so that evaluation happens "
            "at the point where responses start to differ for different prompts. "
            "If not set, the prefix is determined automatically by comparing multiple responses."
        ),
    )

    chain_of_thought_skips: list[tuple[str, str]] = Field(
        default=[
            # Most thinking models.
            (
                "<think>",
                "<think></think>",
            ),
            # gpt-oss.
            (
                "<|channel|>analysis<|message|>",
                "<|channel|>analysis<|message|><|end|><|start|>assistant<|channel|>final<|message|>",
            ),
            # Unknown, suggested by user.
            (
                "<thought>",
                "<thought></thought>",
            ),
            # Unknown, suggested by user.
            (
                "[THINK]",
                "[THINK][/THINK]",
            ),
        ],
        description=(
            "List of pairs of the form (cot_initializer, closed_cot_block) used to skip "
            "the Chain-of-Thought block in responses, so that evaluation happens "
            "at the start of the actual response."
        ),
        # When storing a settings object, the response prefix is already fixed,
        # either determined by the automatic mechanism or by explicit user choice.
        exclude=True,
    )

    print_debug_information: bool = Field(
        default=False,
        description="Whether to print additional information that can help with debugging.",
        exclude=True,
    )

    print_residual_geometry: bool = Field(
        default=False,
        description="Whether to print detailed information about residuals and residual directions.",
        exclude=True,
    )

    plot_residuals: bool = Field(
        default=False,
        description="Whether to generate plots showing PaCMAP projections of residual vectors.",
        exclude=True,
    )

    residual_plot_path: str = Field(
        default="plots",
        description="Base path to save plots of residual vectors to.",
        exclude=True,
    )

    residual_plot_title: str = Field(
        default='PaCMAP Projection of Residual Vectors for "Harmless" and "Harmful" Prompts',
        description="Title placed above plots of residual vectors.",
        exclude=True,
    )

    residual_plot_style: str = Field(
        default="dark_background",
        description="Matplotlib style sheet to use for plots of residual vectors.",
        exclude=True,
    )

    scorers: list[ScorerConfig] = Field(
        default_factory=lambda: [
            ScorerConfig(
                plugin="heretic.scorers.keyword_rate.KeywordRate",
                optimization="minimize",
            ),
            ScorerConfig(
                plugin="heretic.scorers.kl_divergence.KLDivergence",
                optimization="minimize",
            ),
        ],
        description=(
            "List of scorer plugin configs. Each entry is an object"
            " { plugin = <plugin>, optimization = <optimization>, instance_name = <optional> }."
            " <optimization> is one of 'minimize', 'maximize', 'none' (do not optimize)."
        ),
    )

    orthogonalize_direction: bool = Field(
        default=True,
        description=(
            "Whether to adjust the residual directions so that only the component that is "
            "orthogonal to the good direction is subtracted during abliteration."
        ),
    )

    row_normalization: RowNormalization = Field(
        default=RowNormalization.FULL,
        description=(
            "How to apply row normalization of the weights. Options: "
            '"none" (no normalization), '
            '"pre" (compute LoRA adapter relative to row-normalized weights), '
            '"full" (like "pre", but renormalizes to preserve original row magnitudes).'
        ),
    )

    full_normalization_lora_rank: PositiveInt = Field(
        default=3,
        description=(
            'The rank of the LoRA adapter to use when "full" row normalization is used. '
            "Row magnitude preservation is approximate due to non-linear effects, "
            "and this determines the rank of that approximation. Higher ranks produce "
            "larger output files and may slow down evaluation."
        ),
    )

    target_components: list[str] = Field(
        default=["attn.o_proj", "mlp.down_proj"],
        description="Ordered logical projection components to modify.",
    )

    ara_lora_rank: PositiveInt = Field(
        default=128,
        description="LoRA rank used for Calibrated Arbitrary-Rank Ablation.",
    )

    ara_calibration_size: int = Field(
        default=64,
        ge=2,
        description="Number of good and bad prompts retained for CARA calibration.",
    )

    ara_capture_batch_size: PositiveInt = Field(
        default=1,
        description="Dedicated batch size for target-module I/O capture.",
    )

    ara_softmin_temperature: float = Field(
        default=0.10,
        gt=0,
        description="Temperature for CARA soft nearest-neighbor distances.",
    )

    ara_lbfgs_max_iter: PositiveInt = Field(
        default=20,
        description="Maximum L-BFGS iterations for each CARA target module.",
    )

    ara_lbfgs_history_size: PositiveInt = Field(
        default=10,
        description="L-BFGS history size for each CARA target module.",
    )

    acceptance_gate: AcceptanceGate | None = Field(
        default=None,
        description="Optional deterministic candidate-selection and audit gate.",
    )

    winsorization_quantile: float = Field(
        default=1.0,
        description=(
            "The symmetric winsorization to apply to the per-prompt, per-layer residual vectors, "
            "expressed as the quantile to clamp to (between 0 and 1). Disabled by default. "
            'This can tame so-called "massive activations" that occur in some models. '
            "Example: winsorization_quantile = 0.95 computes the 0.95-quantile of the absolute values "
            "of the components, then clamps the magnitudes of all components to that quantile."
        ),
    )

    n_trials: PositiveInt = Field(
        default=200,
        description="Number of abliteration trials to run during optimization.",
    )

    n_startup_trials: NonNegativeInt = Field(
        default=60,
        description="Number of trials that use random sampling for the purpose of exploration.",
    )

    seed: int | None = Field(
        default=None,
        description=(
            "Random seed for reproducible optimization. "
            "Applies to Python's random module, NumPy, PyTorch, and Optuna."
        ),
    )

    study_checkpoint_dir: str = Field(
        default="checkpoints",
        description="Directory to save and load study progress to/from.",
        exclude=True,
    )

    benchmarks: list[BenchmarkSpecification] = Field(
        default=[
            BenchmarkSpecification(
                task="agieval",
                name="AGIEval",
                description="A Human-Centric Benchmark for Evaluating Foundation Models",
            ),
            BenchmarkSpecification(
                task="bbh",
                name="BIG-Bench Hard (BBH)",
                description="Challenging BIG-Bench Tasks and Whether Chain-of-Thought Can Solve Them",
            ),
            BenchmarkSpecification(
                task="commonsense_qa",
                name="CommonsenseQA",
                description="A Question Answering Challenge Targeting Commonsense Knowledge",
            ),
            BenchmarkSpecification(
                task="eq_bench",
                name="EQ-Bench",
                description="An Emotional Intelligence Benchmark for Large Language Models",
            ),
            BenchmarkSpecification(
                task="gsm8k",
                name="GSM8K",
                description="Training Verifiers to Solve Math Word Problems",
            ),
            BenchmarkSpecification(
                task="hellaswag",
                name="HellaSwag",
                description="Can a Machine Really Finish Your Sentence?",
            ),
            BenchmarkSpecification(
                task="ifeval",
                name="IFEval",
                description="Instruction-Following Evaluation for Large Language Models",
            ),
            BenchmarkSpecification(
                task="mmlu",
                name="MMLU",
                description="Measuring Massive Multitask Language Understanding",
            ),
            BenchmarkSpecification(
                task="mmlu_pro",
                name="MMLU-Pro",
                description="A More Robust and Challenging Multi-Task Language Understanding Benchmark",
            ),
            BenchmarkSpecification(
                task="piqa",
                name="PIQA",
                description="Reasoning about Physical Commonsense in Natural Language",
            ),
            BenchmarkSpecification(
                task="winogrande",
                name="WinoGrande",
                description="An Adversarial Winograd Schema Challenge at Scale",
            ),
        ],
        description="Benchmarks to offer to the user for evaluating abliterated models.",
        exclude=True,
    )

    max_shard_size: PositiveInt | str = Field(
        default="5GB",
        description="Maximum size for individual safetensors files generated when exporting a model.",
    )

    export_strategy: ExportStrategy | None = Field(
        default=None,
        description='How to export the model: "merge", "adapter", or unset to prompt the user.',
    )

    checkpoint_action: str | None = Field(
        default=None,
        description='Action to take in case a checkpoint exists: "continue", "restart", or unset to prompt the user.',
    )

    trial_index: NonNegativeInt | None = Field(
        default=None,
        description="Index (in the sorted Pareto front) of the trial to use, or unset to prompt the user.",
    )

    n_additional_trials: PositiveInt | None = Field(
        default=None,
        description="Number of additional trials to run, or unset to prompt the user.",
    )

    model_action: str | None = Field(
        default=None,
        description='Action to take with the decensored model: "save", "upload", or unset to prompt the user.',
    )

    save_directory: str | None = Field(
        default=None,
        description="Directory to save the model to, or unset to prompt the user.",
        exclude=True,
    )

    upload_repo_id: str | None = Field(
        default=None,
        description="Name of the Hugging Face repository to upload the model to, or unset to prompt the user.",
        exclude=True,
    )

    upload_repo_private: bool | None = Field(
        default=None,
        description="Whether the Hugging Face repository to upload the model to should be private, or unset to prompt the user.",
    )

    upload_reproducibility_information: str | None = Field(
        default=None,
        description='Which reproducibility information to add to the Hugging Face repository: "full", "basic", "none", or unset to prompt the user.',
    )

    ignore_mismatches: bool | None = Field(
        default=None,
        description="Whether to attempt to reproduce the model even if there are environment mismatches, or unset to prompt the user.",
    )

    system_prompt: str = Field(
        default="You are a helpful assistant.",
        description="System prompt to use when prompting the model.",
    )

    good_prompts: DatasetSpecification = Field(
        default=DatasetSpecification(
            dataset="mlabonne/harmless_alpaca",
            split="train[:400]",
            column="text",
            residual_plot_label='"Harmless" prompts',
            residual_plot_color="royalblue",
        ),
        description="Dataset of prompts that tend to not result in refusals (used for calculating refusal directions).",
    )

    bad_prompts: DatasetSpecification = Field(
        default=DatasetSpecification(
            dataset="mlabonne/harmful_behaviors",
            split="train[:400]",
            column="text",
            residual_plot_label='"Harmful" prompts',
            residual_plot_color="darkorange",
        ),
        description="Dataset of prompts that tend to result in refusals (used for calculating refusal directions).",
    )

    @field_validator("target_components")
    @classmethod
    def validate_target_components(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("target_components must not be empty")
        supported = {"attn.o_proj", "mlp.down_proj"}
        unknown = [component for component in value if component not in supported]
        if unknown:
            raise ValueError(f"unsupported target components: {unknown}")
        return list(dict.fromkeys(value))

    @field_validator("chat_template_kwargs")
    @classmethod
    def validate_chat_template_kwargs(cls, value: dict[str, Any]) -> dict[str, Any]:
        reserved = {"tokenize", "add_generation_prompt", "continue_final_message"}
        conflicts = sorted(reserved & value.keys())
        if conflicts:
            raise ValueError(f"reserved chat template keys: {conflicts}")
        return value

    @field_validator("generation_kwargs")
    @classmethod
    def validate_generation_kwargs(cls, value: dict[str, Any]) -> dict[str, Any]:
        reserved = {"pad_token_id", "streamer", "input_ids", "attention_mask"}
        conflicts = sorted(reserved & value.keys())
        if conflicts:
            raise ValueError(f"reserved generation keys: {conflicts}")
        return value

    @model_validator(mode="after")
    def validate_method_settings(self) -> "Settings":
        if (
            self.abliteration_method == AbliterationMethod.ARA
            and self.row_normalization != RowNormalization.NONE
        ):
            raise ValueError('row_normalization must be "none" for ARA')
        if self.acceptance_gate is not None:
            if self.abliteration_method != AbliterationMethod.ARA:
                raise ValueError("acceptance_gate is only supported for ARA")
            if self.generation_kwargs.get("do_sample", False) is not False:
                raise ValueError("acceptance_gate requires generation do_sample=false")
            if (
                self.model == "Qwen/Qwen3.8-27B"
                and self.chat_template_kwargs.get("enable_thinking") is not False
            ):
                raise ValueError("Qwen acceptance requires enable_thinking=false")
            identities = [
                (config.plugin, config.instance_name) for config in self.scorers
            ]
            if len(identities) != len(set(identities)):
                raise ValueError("acceptance gate requires unique scorer instances")
            candidates = [
                self.good_prompts,
                self.bad_prompts,
                *_acceptance_scorer_prompts(
                    self.scorers, self.model_extra, self.acceptance_gate
                ),
                *_configured_dataset_specs(self.model_extra),
            ]
            if any(not item.commit for item in candidates):
                raise ValueError("acceptance datasets must pin a commit")
            audits = (
                self.acceptance_gate.keyword_audit_prompts,
                self.acceptance_gate.kl_audit_prompts,
            )
            if any(
                dataset_specs_overlap(audit, item)
                for audit in audits
                for item in candidates
            ):
                raise ValueError(
                    "acceptance audit rows overlap calibration or validation"
                )
        return self

    # Extra keys hold plugin configuration such as `[scorer.KeywordRate]`.
    model_config = SettingsConfigDict(extra="allow")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,  # Used during resume - should override *all* other sources.
            CliSettingsSource(
                settings_cls,
                cli_parse_args=True,
                cli_implicit_flags=True,
                cli_kebab_case=True,
            ),
            EnvSettingsSource(settings_cls, env_prefix="HERETIC_"),
            dotenv_settings,
            file_secret_settings,
            TomlConfigSettingsSource(settings_cls, toml_file="config.toml"),
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
        "reproduce",
        "save_directory",
        "study_checkpoint_dir",
        "trial_index",
        "upload_repo_id",
        "upload_repo_private",
        "upload_reproducibility_information",
    }
)


def merge_study_settings(stored: Settings, current: Settings) -> Settings:
    """Restore research settings while retaining current run-control choices."""

    values = {name: getattr(stored, name) for name in Settings.model_fields}
    values.update(stored.model_extra or {})
    if stored.acceptance_gate is not None and current.acceptance_gate is not None:
        values["acceptance_gate"] = stored.acceptance_gate.model_copy(
            update={"report_path": current.acceptance_gate.report_path}
        )
    for field_name in RUN_CONTROL_FIELDS:
        values[field_name] = getattr(current, field_name)
    return Settings.model_validate(values)
