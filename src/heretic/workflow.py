# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026  Philipp Emanuel Weidmann <pew@worldwidemann.com> + contributors

"""High-level preparation, acceptance, and export workflow helpers."""

import hashlib
import json
import multiprocessing
import os
import platform
import queue
import warnings
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, cast

import psutil
import questionary
import torch
import torch.nn.functional as F
from optuna.trial import FrozenTrial, create_trial
from questionary import Choice, Style

from .analyzer import Analyzer
from .ara import (
    AdapterInitialState,
    ARAArtifacts,
    CalibrationManifest,
    TargetModule,
    get_lora_factors,
)
from .ara_runtime import validate_runtime_guard
from .config import ExportStrategy, QuantizationMethod, Settings
from .evaluator import Evaluator
from .model import Model, get_model_class
from .system import empty_cache, get_accelerator_info_dict
from .trial_methods import (
    AcceptanceGateError,
    AcceptanceReport,
    DirectionalArtifacts,
    MethodArtifacts,
    apply_trial,
    cleanup_trial,
    parameter_envelope,
    parameters_from_trial,
    parse_parameter_envelope,
    select_accepted_trial,
    validate_gate_records,
    write_acceptance_report,
)
from .utils import (
    Prompt,
    ask_if_unset,
    create_reproduce_folder,
    generate_config_toml,
    get_file_sha256,
    get_readme_intro,
    load_prompts,
    print,
)

_RELOAD_SMOKE_PROMPTS = (
    "What is 1 + 1?",
    "Name the capital of France.",
    "Write one sentence about the ocean.",
    "List three primary colors.",
    'Translate "hello" into Spanish.',
    "What season follows spring?",
    "Give a short definition of gravity.",
    "Name one programming language.",
    "What is the chemical symbol for water?",
    "Write a polite greeting.",
)


def configure_runtime_determinism() -> None:
    """Enable deterministic controls supported by the active Torch backend."""
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    torch.use_deterministic_algorithms(True, warn_only=True)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True


def run_additional_trials(
    settings: Settings,
    study: Any,
    objective: Any,
) -> None:
    """Prompt for and execute additional point-v1 optimization trials."""
    while True:
        value = ask_if_unset(
            settings.n_additional_trials,
            questionary.text("How many additional trials do you want to run?"),
        )
        if value in (None, ""):
            return
        try:
            count = int(value)
        except ValueError:
            print("[red]Please enter a number.[/]")
            continue
        if count > 0:
            break
        print("[red]Please enter a number greater than 0.[/]")
    settings.n_trials = len(study.trials) + count
    study.set_user_attr("settings", settings.model_dump_json())
    study.set_user_attr("finished", False)
    try:
        study.optimize(objective, n_trials=count)
    except KeyboardInterrupt:
        pass
    if len(study.trials) == settings.n_trials:
        study.set_user_attr("finished", True)


def load_reproduction_acceptance(
    source: str, reproduction: Mapping[str, Any], required: bool
) -> dict[str, Any] | None:
    """Load and verify the acceptance report bound to reproduce.json."""
    from .artifact_schema import load_bound_acceptance

    try:
        return load_bound_acceptance(source, reproduction, required)
    except ValueError as error:
        raise AcceptanceGateError(str(error)) from error


def validate_reproduction_model(model: Model, report: Mapping[str, Any] | None) -> None:
    """Reject a loaded base model that differs from its acceptance report."""
    if (
        report is not None
        and report.get("model_fingerprint") != model.model_fingerprint
    ):
        raise AcceptanceGateError(
            "loaded model fingerprint does not match accepted reproduction"
        )


def validate_reproduction_artifacts(
    reproduction: Mapping[str, Any] | None,
    artifacts: Any,
    study_fingerprint: str,
) -> None:
    """Bind a regenerated trajectory and study identity before v2 apply."""
    if reproduction is None or reproduction.get("schema") != "cara-reproduce-v2":
        return
    if reproduction.get("study_fingerprint") != study_fingerprint:
        raise AcceptanceGateError("reproduced study fingerprint differs")
    if getattr(artifacts, "trajectory_fingerprint", None) != reproduction.get(
        "trajectory_manifest_sha256"
    ):
        raise AcceptanceGateError("reproduced trajectory fingerprint differs")


def make_reproduction_trial(
    reproduction: Mapping[str, Any], normalized: Mapping[str, Any]
) -> FrozenTrial:
    """Build a method-aware frozen trial from validated reproduction data."""
    envelope = parameter_envelope(parse_parameter_envelope(normalized))
    attrs = {
        "index": 0,
        "method": envelope["method"],
        "scores": reproduction.get("scores", []),
        "model_fingerprint": reproduction.get("model_fingerprint"),
        "study_fingerprint": reproduction.get("study_fingerprint"),
        "calibration_fingerprint": reproduction.get("calibration_fingerprint"),
    }
    if envelope["method"] == "ara":
        if envelope.get("objective_version") == "trajectory-v2":
            attrs["ara_parameters"] = envelope
            attrs["objective_version"] = "trajectory-v2"
            attrs["search_space_version"] = envelope["search_space_version"]
            attrs["trajectory_fingerprint"] = reproduction.get(
                "trajectory_manifest_sha256"
            )
        else:
            attrs["ara_parameters"] = envelope["payload"]
    else:
        attrs["direction_index"] = envelope["payload"]["direction_index"]
        attrs["parameters"] = envelope["payload"]["abliteration_parameters"]
    return create_trial(values=[], user_attrs=attrs)


def _capture_adapter_state(targets: tuple[TargetModule, ...]) -> AdapterInitialState:
    tensors = {}
    for target in targets:
        for factor, suffix in zip(get_lora_factors(target), ("lora_A", "lora_B")):
            name = f"{target.full_name}.{suffix}.default.weight"
            tensors[name] = factor.detach().to("cpu", torch.float32).clone()
    return AdapterInitialState(tensors, tuple(sorted(tensors)))


def _adapter_states_allclose(
    left: AdapterInitialState, right: AdapterInitialState
) -> bool:
    if left.ordered_parameter_names != right.ordered_parameter_names:
        return False
    return all(
        torch.allclose(left.tensors[name], right.tensors[name], rtol=1e-6, atol=1e-7)
        for name in left.ordered_parameter_names
    )


def _print_quantized_export_warning(settings: Settings, model: Model) -> None:
    print()
    print(
        "The model was loaded with quantization. Merging requires reloading the base model."
    )
    print("[yellow]WARNING: CPU merging requires dequantizing the model to RAM.[/]")
    print("[yellow]This can lead to system freezes if you run out of memory.[/]")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model_class = get_model_class(
                settings.model, revision=settings.model_commit
            )
            meta_model = model_class.from_pretrained(
                settings.model,
                device_map="meta",
                torch_dtype=torch.bfloat16,
                trust_remote_code=True
                if settings.model in model.trusted_models
                else None,
                **model.revision_kwargs,
            )
            footprint_gb = meta_model.get_memory_footprint() / (1024**3)
            print(
                "[yellow]Estimated RAM required (excluding overhead): "
                f"[bold]~{footprint_gb:.2f} GB[/][/]"
            )
    except Exception:
        print(
            "[yellow]Rule of thumb: allow roughly 3x the parameter count in GB RAM.[/]"
        )
    print()


def obtain_export_strategy(settings: Settings, model: Model) -> ExportStrategy | None:
    """Resolve the requested export strategy, including quantized-model guidance."""
    if settings.export_strategy is not None:
        return settings.export_strategy
    if settings.quantization == QuantizationMethod.BNB_4BIT:
        _print_quantized_export_warning(settings, model)
    return ask_if_unset(
        None,
        questionary.select(
            "How do you want to export the model?",
            choices=[
                Choice(
                    title="Merge the abliteration LoRA and export the full model"
                    + (
                        ""
                        if settings.quantization == QuantizationMethod.NONE
                        else " (requires sufficient RAM)"
                    ),
                    value=ExportStrategy.MERGE,
                ),
                Choice(
                    title="Export the abliteration LoRA only (can be merged later)",
                    value=ExportStrategy.ADAPTER,
                ),
            ],
            style=Style([("highlighted", "reverse")]),
        ),
    )


def prepare_directional_artifacts(
    settings: Settings,
    model: Model,
    good_prompts: list,
    bad_prompts: list,
) -> DirectionalArtifacts:
    """Calculate legacy refusal directions without retaining unused residuals."""
    print()
    print("Calculating per-layer residual directions...")
    needs_full = settings.print_residual_geometry or settings.plot_residuals
    if needs_full:
        print("* Obtaining residuals for good prompts...")
        good_residuals = model.get_residuals_batched(good_prompts)
        print("* Obtaining residuals for bad prompts...")
        bad_residuals = model.get_residuals_batched(bad_prompts)
        good_means, bad_means = good_residuals.mean(0), bad_residuals.mean(0)
        analyzer = Analyzer(settings, model, good_residuals, bad_residuals)
        if settings.print_residual_geometry:
            analyzer.print_residual_geometry()
        if settings.plot_residuals:
            analyzer.plot_residuals()
    else:
        print("* Obtaining residual mean for good prompts...")
        good_means = model.get_residuals_mean(good_prompts)
        print("* Obtaining residual mean for bad prompts...")
        bad_means = model.get_residuals_mean(bad_prompts)
    directions = F.normalize(bad_means - good_means, p=2, dim=1)
    if settings.orthogonalize_direction:
        good_directions = F.normalize(good_means, p=2, dim=1)
        projection = torch.sum(directions * good_directions, dim=1)
        directions = F.normalize(
            directions - projection.unsqueeze(1) * good_directions, p=2, dim=1
        )
    return DirectionalArtifacts(directions)


def _prompt_digest(prompt: Prompt) -> str:
    normalized = json.dumps(
        {"system": prompt.system, "user": prompt.user},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(normalized.encode()).hexdigest()


def _sample_indices(length: int, size: int, seed: int, side: str) -> tuple[int, ...]:
    if size < 2 or length < size:
        raise ValueError(f"{side} calibration requires at least {size} prompts")
    digest = hashlib.sha256(f"{seed}:ara-calibration:{side}".encode()).digest()
    generator = torch.Generator(device="cpu")
    generator.manual_seed(int.from_bytes(digest[-8:], "big"))
    return tuple(
        int(item) for item in torch.randperm(length, generator=generator)[:size]
    )


def select_calibration_prompts(
    good_prompts: list[Prompt], bad_prompts: list[Prompt], size: int, seed: int
) -> tuple[list[Prompt], list[Prompt], CalibrationManifest]:
    """Select stable calibration samples without replacement."""
    good_indices = _sample_indices(len(good_prompts), size, seed, "good")
    bad_indices = _sample_indices(len(bad_prompts), size, seed, "bad")
    selected_good = [good_prompts[index] for index in good_indices]
    selected_bad = [bad_prompts[index] for index in bad_indices]
    return (
        selected_good,
        selected_bad,
        CalibrationManifest(
            good_indices=good_indices,
            bad_indices=bad_indices,
            good_prompt_sha256=tuple(map(_prompt_digest, selected_good)),
            bad_prompt_sha256=tuple(map(_prompt_digest, selected_bad)),
            seed=seed,
            capture_batch_size=1,
        ),
    )


@dataclass(frozen=True)
class AcceptanceRuntime:
    """Objects shared by validation replay and audit evaluation."""

    settings: Settings
    model: Model
    artifacts: MethodArtifacts
    evaluator: Evaluator


def audit_settings(settings: Settings) -> Settings:
    """Return settings with scorer prompt sources replaced by audit splits."""
    audit = settings.model_copy(deep=True)
    assert audit.acceptance_gate is not None
    if audit.model_extra is None:
        audit.__pydantic_extra__ = {}
    tables = cast(dict[str, Any], audit.model_extra).setdefault("scorer", {})
    gate = audit.acceptance_gate
    for scorer in audit.scorers:
        class_name = scorer.plugin.rsplit(".", 1)[-1]
        namespace = class_name + (
            f"_{scorer.instance_name}" if scorer.instance_name else ""
        )
        display = {
            "KeywordRate": "Keywords",
            "KLDivergence": "KL divergence",
            "RefusalLogOdds": "Refusal log-odds",
        }.get(class_name)
        if scorer.instance_name and display:
            display += f" - {scorer.instance_name}"
        if display == gate.keyword_score:
            tables.setdefault(namespace, {})["prompts"] = (
                gate.keyword_audit_prompts.model_dump()
            )
        elif display == gate.kl_score:
            tables.setdefault(namespace, {})["prompts"] = (
                gate.kl_audit_prompts.model_dump()
            )
        elif class_name == "RefusalLogOdds":
            tables.setdefault(namespace, {})["prompts"] = (
                gate.keyword_audit_prompts.model_dump()
            )
    return audit


def replay_candidate(
    runtime: AcceptanceRuntime, trial: FrozenTrial
) -> tuple[list[dict[str, Any]], Any]:
    """Apply, score, snapshot, and clean up one deterministic candidate replay."""
    apply_trial(runtime.model, parameters_from_trial(trial), runtime.artifacts)
    try:
        state = _capture_adapter_state(runtime.model.ara_targets)
        records = runtime.evaluator.get_paired_score_records(
            runtime.evaluator.get_scores()
        )
        return records, state
    finally:
        cleanup_trial(runtime.model, runtime.artifacts)


def run_acceptance_gate(
    runtime: AcceptanceRuntime,
    trial: FrozenTrial,
) -> list[dict[str, Any]]:
    """Require two stable validation replays followed by a clean audit pass."""
    gate = runtime.settings.acceptance_gate
    assert gate is not None
    first_records, first_state = replay_candidate(runtime, trial)
    second_records, second_state = replay_candidate(runtime, trial)
    first_values = validate_gate_records(first_records, gate)
    second_values = validate_gate_records(second_records, gate)
    adapters_equal = _adapter_states_allclose(first_state, second_state)
    if not adapters_equal:
        raise AcceptanceGateError("candidate adapter replay drifted")
    if (
        first_values[0] != second_values[0]
        or abs(first_values[1] - second_values[1]) > 0.005
    ):
        raise AcceptanceGateError("candidate validation scores drifted")
    evaluator = Evaluator(audit_settings(runtime.settings), runtime.model)
    apply_trial(runtime.model, parameters_from_trial(trial), runtime.artifacts)
    _validate_audit_outputs(runtime.settings, runtime.model)
    records = evaluator.get_paired_score_records(evaluator.get_scores())
    validate_gate_records(records, gate)
    trial.user_attrs["validation_replay_scores"] = [first_records, second_records]
    trial.user_attrs["adapter_replays_equal"] = adapters_equal
    return records


def select_for_acceptance(
    trials: list[FrozenTrial],
    gate: Any,
    fingerprint: str,
    model_name: str | None = None,
) -> FrozenTrial:
    """Apply configured study-health thresholds independent of model path."""
    del model_name
    if len(trials) < gate.required_trials:
        raise AcceptanceGateError(
            f"only {len(trials)} trials ran; {gate.required_trials} required"
        )
    complete = sum(item.state.name == "COMPLETE" for item in trials)
    failures = sum(_is_runtime_failure(item) for item in trials)
    if complete < gate.min_complete_trials:
        raise AcceptanceGateError(
            f"only {complete} trials completed; {gate.min_complete_trials} required"
        )
    if failures / gate.required_trials > gate.max_runtime_failure_rate:
        raise AcceptanceGateError("runtime failure rate exceeds the configured gate")
    return select_accepted_trial(trials, gate, fingerprint)


def ensure_acceptance_study_unlocked(settings: Settings, checkpoint: str) -> None:
    """Prevent additional trials or audit reuse after candidate identity is locked."""

    selection = Path(checkpoint).with_suffix(".selection.jsonl")
    ledger = Path(checkpoint).parent / "audit-evidence" / "audit-ledger.json"
    consumed = False
    if ledger.is_file():
        consumed = bool(
            json.loads(ledger.read_text(encoding="utf-8")).get("audit_consumed")
        )
    if settings.acceptance_gate is not None and selection.exists() and consumed:
        raise AcceptanceGateError(
            f"acceptance candidate is already locked in {selection.name}"
        )


def _is_runtime_failure(trial: FrozenTrial) -> bool:
    structured = trial.user_attrs.get("failure_record")
    if isinstance(structured, dict):
        return structured.get("is_runtime_failure") is True
    record = trial.user_attrs.get("failure")
    if not isinstance(record, dict):
        return False
    text = " ".join(str(record.get(key, "")) for key in ("type", "stage", "message"))
    lowered = text.lower()
    return record.get("stage") == "oom" or any(
        marker in lowered for marker in ("non-finite", "nan", "device")
    )


def _validate_audit_outputs(settings: Settings, model: Model) -> None:
    gate = settings.acceptance_gate
    assert gate is not None
    prompts = load_prompts(settings, gate.keyword_audit_prompts)
    responses = model.get_responses_batched(prompts)
    if len(responses) != gate.expected_samples or any(
        not item.strip() for item in responses
    ):
        raise AcceptanceGateError("audit harmful responses must all be non-empty")
    if not torch.isfinite(model.get_logits_batched(prompts)).all():
        raise AcceptanceGateError("audit harmful logits contain NaN or Inf")


def _reload_worker(
    settings_values: dict[str, Any],
    adapter_path: str,
    smoke_prompts: tuple[str, ...],
    results: Any,
    audit_identity: Mapping[str, str | None] | None = None,
) -> None:
    try:
        from .ara_runtime import adapter_state_identity, capture_named_adapter_state

        settings = Settings.model_validate(settings_values)
        configure_runtime_determinism()
        model = Model(settings)
        model.load_adapter_for_evaluation(adapter_path)
        adapter_identity = adapter_state_identity(
            capture_named_adapter_state(model.model, "candidate")
        )
        expected_adapter = (audit_identity or {}).get("adapter")
        if expected_adapter is not None and adapter_identity != expected_adapter:
            raise RuntimeError("reloaded adapter state identity differs")
        prompts = [Prompt(settings.system_prompt, text) for text in smoke_prompts]
        responses = model.get_responses_batched(prompts)
        logits = model.get_logits_batched(prompts)
        if len(responses) != 10 or any(not response.strip() for response in responses):
            raise RuntimeError("adapter reload smoke produced an empty response")
        if not torch.isfinite(logits).all():
            raise RuntimeError("adapter reload smoke produced non-finite logits")
        ledger_path = (audit_identity or {}).get("ledger")
        if ledger_path is not None:
            from .acceptance_export import transition_audit_ledger

            transition_audit_ledger(ledger_path, "consumed")
        with model.model.disable_adapter():  # ty:ignore[call-non-callable]
            evaluator = Evaluator(audit_settings(settings), model)
        records = evaluator.get_paired_score_records(evaluator.get_scores())
        final_identity = adapter_state_identity(
            capture_named_adapter_state(model.model, "candidate")
        )
        if final_identity != adapter_identity:
            raise RuntimeError("audit changed the loaded adapter state identity")
        assert settings.acceptance_gate is not None
        validate_gate_records(records, settings.acceptance_gate)
        results.put(
            {"status": "passed", "scores": records, "model": model.model_fingerprint}
        )
    except BaseException as error:
        results.put({"status": "failed", "reason": str(error)})


def verify_reloaded_adapter(
    settings: Settings,
    adapter_path: Path,
    expected_model: str,
    ledger_path: Path | None = None,
    expected_adapter: str | None = None,
) -> list[dict[str, Any]]:
    """Reload the staged adapter in a fresh spawned process and run audit/smoke."""
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    process = context.Process(
        target=_reload_worker,
        args=(
            settings.model_dump(),
            str(adapter_path),
            _RELOAD_SMOKE_PROMPTS,
            results,
            {
                "ledger": str(ledger_path) if ledger_path is not None else None,
                "adapter": expected_adapter,
            },
        ),
    )
    process.start()
    process.join(timeout=3600)
    if process.is_alive():
        process.terminate()
        process.join()
        raise TimeoutError("adapter reload verification timed out")
    try:
        report = results.get_nowait()
    except queue.Empty as error:
        raise RuntimeError(
            f"adapter reload process exited with code {process.exitcode}"
        ) from error
    if process.exitcode != 0 or report.get("status") != "passed":
        raise RuntimeError(report.get("reason", "adapter reload verification failed"))
    if report.get("model") != expected_model:
        raise RuntimeError("adapter reload changed the pinned model fingerprint")
    return cast(list[dict[str, Any]], report["scores"])


@dataclass(frozen=True)
class AcceptedExport:
    """Evidence required to publish a gate-approved adapter atomically."""

    runtime: AcceptanceRuntime
    trial: FrozenTrial
    audit_scores: list[dict[str, Any]]
    study_fingerprint: str
    calibration_fingerprint: str | None
    checkpoint_path: str


def _artifact_hashes(path: Path) -> dict[str, str]:
    return {
        str(file.relative_to(path)).replace("\\", "/"): get_file_sha256(file)
        for file in sorted(path.rglob("*"))
        if file.is_file()
    }


def _save_staging_files(staging: Path, evidence: AcceptedExport) -> None:
    settings, model = evidence.runtime.settings, evidence.runtime.model
    model.model.save_pretrained(str(staging), max_shard_size=settings.max_shard_size)
    model.tokenizer.save_pretrained(staging)
    if model.processor is not None:
        model.processor.save_pretrained(staging)
    (staging / "effective-config.toml").write_text(
        generate_config_toml(settings), encoding="utf-8"
    )
    manifest = cast(ARAArtifacts, evidence.runtime.artifacts).calibration_manifest
    (staging / "calibration-manifest.json").write_text(
        json.dumps(asdict(manifest), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _runtime_diagnostics(
    model: Model,
) -> tuple[dict[str, int], dict[str, float], dict[str, str]]:
    module_counts: dict[str, int] = {}
    for target in model.ara_targets:
        logical = f"logical:{target.key.component}"
        module_counts[logical] = module_counts.get(logical, 0) + 1
        for physical in (
            "self_attn.o_proj",
            "linear_attn.out_proj",
            "mlp.down_proj",
        ):
            if physical in target.full_name:
                module_counts[physical] = module_counts.get(physical, 0) + 1
    cuda_peaks = {
        f"cuda:{index}_allocated_gib": torch.cuda.max_memory_allocated(index) / 1024**3
        for index in range(torch.cuda.device_count())
    }
    resources = {
        "cpu_rss_gib": psutil.Process().memory_info().rss / 1024**3,
        **cuda_peaks,
    }
    if model.settings.ara_runtime_guard is not None:
        report = validate_runtime_guard(
            model.ara_targets,
            model.settings.ara_runtime_guard,
        )
        module_counts = dict(report.target_counts)
    return module_counts, resources, _environment_diagnostics(model)


def _distribution_version(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "uninstalled"


def _environment_diagnostics(model: Model) -> dict[str, str]:
    accelerator = get_accelerator_info_dict()
    actual_map = getattr(model.model, "hf_device_map", {})
    return {
        "heretic": _distribution_version("heretic-llm"),
        "python": platform.python_version(),
        "torch": torch.__version__,
        "transformers": _distribution_version("transformers"),
        "peft": _distribution_version("peft"),
        "bitsandbytes": _distribution_version("bitsandbytes"),
        "cuda": str(torch.version.cuda),
        "driver": str(accelerator.get("driver_version")),
        "accelerator": json.dumps(accelerator, sort_keys=True, default=str),
        "configured_device_map": json.dumps(model.settings.device_map, sort_keys=True),
        "actual_device_map": json.dumps(actual_map, sort_keys=True, default=str),
        "model_commit": str(model.settings.model_commit),
        "deterministic_algorithms": str(torch.are_deterministic_algorithms_enabled()),
        "deterministic_warn_only": str(
            torch.is_deterministic_algorithms_warn_only_enabled()
        ),
        "cudnn_deterministic": str(torch.backends.cudnn.deterministic),
        "cudnn_benchmark": str(torch.backends.cudnn.benchmark),
        "cudnn_version": str(torch.backends.cudnn.version()),
        "cublas_workspace_config": os.environ.get("CUBLAS_WORKSPACE_CONFIG", ""),
    }


def _acceptance_report(
    evidence: AcceptedExport,
    reload_scores: list[dict[str, Any]],
    hashes: dict[str, str],
    diagnostics: tuple[dict[str, int], dict[str, float], dict[str, str]],
) -> AcceptanceReport:
    gate = evidence.runtime.settings.acceptance_gate
    assert gate is not None
    before = validate_gate_records(evidence.audit_scores, gate)
    after = validate_gate_records(reload_scores, gate)
    counts, resources, environment = diagnostics
    return AcceptanceReport(
        status="passed",
        reason="validation replay, audit, and adapter reload passed",
        model_fingerprint=evidence.runtime.model.model_fingerprint,
        study_fingerprint=evidence.study_fingerprint,
        calibration_fingerprint=evidence.calibration_fingerprint,
        selected_trial_number=evidence.trial.number,
        parameters=parameter_envelope(parameters_from_trial(evidence.trial)),
        validation_scores=evidence.trial.user_attrs["scores"],
        audit_scores=evidence.audit_scores,
        reload_scores=reload_scores,
        validation_replay_scores=evidence.trial.user_attrs.get(
            "validation_replay_scores"
        ),
        parameter_comparison={
            "adapter_replays_equal": evidence.trial.user_attrs.get(
                "adapter_replays_equal", False
            )
        },
        score_drift={
            "keyword": abs(before[0] - after[0]),
            "kl": abs(before[1] - after[1]),
        },
        module_counts=counts,
        resource_peaks=resources,
        environment_versions=environment,
        failure_trials=evidence.trial.user_attrs.get("failure_trials"),
        artifact_hashes=hashes,
    )


def _release_model_for_reload(model: Model) -> None:
    """Release parent-process GPU state before the isolated reload check."""

    model.model = None  # ty:ignore[invalid-assignment]
    model.ara_targets = ()
    model.adapter_initial_state = None
    empty_cache()


def _write_accepted_model_card(staging: Path, evidence: AcceptedExport) -> None:
    evidence.trial.user_attrs["acceptance_status"] = "passed"
    contents = get_readme_intro(evidence.runtime.settings, evidence.trial, True)
    (staging / "README.md").write_text(contents, encoding="utf-8")


def export_accepted_adapter(destination: Path, evidence: AcceptedExport) -> None:
    """Verify and atomically publish an accepted CARA adapter bundle."""
    settings, gate = (
        evidence.runtime.settings,
        evidence.runtime.settings.acceptance_gate,
    )
    assert gate is not None
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {destination}")
    staging = destination.with_name(f".{destination.name}.staging")
    if staging.exists():
        raise FileExistsError(f"staging directory already exists: {staging}")
    staging.mkdir(parents=True)
    _save_staging_files(staging, evidence)
    diagnostics = _runtime_diagnostics(evidence.runtime.model)
    expected_model = evidence.runtime.model.model_fingerprint
    _release_model_for_reload(evidence.runtime.model)
    reload_scores = verify_reloaded_adapter(settings, staging, expected_model)
    before, after = (
        validate_gate_records(records, gate)
        for records in (evidence.audit_scores, reload_scores)
    )
    if abs(before[0] - after[0]) > 1 / gate.expected_samples:
        raise AcceptanceGateError("reload keyword score drift exceeded one sample")
    if abs(before[1] - after[1]) > 0.005:
        raise AcceptanceGateError("reload KL drift exceeded 0.005")
    _write_accepted_model_card(staging, evidence)
    hashes = _artifact_hashes(staging)
    report = _acceptance_report(evidence, reload_scores, hashes, diagnostics)
    write_acceptance_report(staging / "acceptance.json", report)
    reproduction_settings = settings.model_copy(deep=True)
    assert reproduction_settings.acceptance_gate is not None
    reproduction_settings.acceptance_gate.report_path = str(staging / "acceptance.json")
    create_reproduce_folder(
        staging,
        reproduction_settings,
        evidence.checkpoint_path,
        evidence.trial,
        uploaded_model_hashes=hashes,
        include_system_information=True,
    )
    staging.rename(destination)
    configured_report = Path(gate.report_path)
    if configured_report.resolve() != (destination / "acceptance.json").resolve():
        write_acceptance_report(configured_report, report)
