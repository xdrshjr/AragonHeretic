# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026  Philipp Emanuel Weidmann <pew@worldwidemann.com> + contributors

# ruff: noqa: E402

import sys

# Ensure standard output/error use UTF-8 instead of system default charmap (e.g. cp1252 on Windows).
for stream in (sys.stdout, sys.stderr):
    if (
        hasattr(stream, "reconfigure")
        and (getattr(stream, "encoding", "") or "").lower() != "utf-8"
    ):
        stream.reconfigure(encoding="utf-8")  # type: ignore

from .config import Settings


def _is_help_invocation() -> bool:
    args = sys.argv[1:]
    return "-h" in args or "--help" in args


# Parse and handle CLI help before importing heavyweight ML/runtime dependencies.
if _is_help_invocation():
    Settings()  # ty:ignore[missing-argument]

# FIXME: Rich progress bars are currently disabled because of rendering issues
#        when used from multiple threads in parallel (e.g. by huggingface_hub).
"""
from .progress import patch_tqdm

# This patches tqdm class definitions, which must happen
# before any other module imports tqdm.
patch_tqdm()
"""

import logging
import math
import os
import random
import time
import warnings
from dataclasses import dataclass, replace
from importlib.metadata import version
from os.path import commonprefix
from pathlib import Path
from typing import Any, Mapping, cast

import optuna
import questionary
import torch
import transformers
from optuna.exceptions import ExperimentalWarning
from optuna.storages import JournalStorage
from optuna.storages.journal import JournalFileBackend, JournalFileOpenLock
from optuna.study import StudyDirection
from pydantic import ValidationError
from questionary import Choice, Style
from rich.traceback import install

from . import (
    study_runner,
    trial_methods,
    workflow,
)
from .ara_config import configured_dataset_specs
from .config import (
    AbliterationMethod,
    merge_study_settings,
)
from .evaluator import Evaluator
from .model import Model
from .protocol_data import (
    RoleDataset,
    materialize_prompt_bundle,
    preflight_audit_metadata,
    prompt_spec_cache_key,
    validate_role_overlaps,
    validate_role_sizes,
)
from .reproduce import (
    check_environment,
    collect_reproducibles,
    load_reproduction_information,
)
from .system import get_accelerator_info
from .utils import (
    ask_if_unset,
    format_exception,
    load_prompts,
    print,
    print_memory_usage,
)


@dataclass(frozen=True)
class ReproductionState:
    """Validated reproduction inputs and the effective settings."""

    settings: Settings
    information: Mapping[str, Any] | None
    parameters: Mapping[str, Any] | None
    acceptance_report: Mapping[str, Any] | None


@dataclass(frozen=True)
class CheckpointState:
    """Resolved journal after the configured resume/restart decision."""

    settings: Settings
    path: Path
    storage: JournalStorage


@dataclass(frozen=True)
class PromptState:
    """Loaded calibration prompts, cache, and optional ARA manifest."""

    good: list[Any]
    bad: list[Any]
    cache: dict[Any, list[Any]]
    calibration_manifest: Any | None


def _print_banner() -> None:
    # Modified "Pagga" font from https://budavariam.github.io/asciiart-text/
    print(f"[cyan]█░█░█▀▀░█▀▄░█▀▀░▀█▀░█░█▀▀[/]  v{version('heretic-llm')}")
    print(
        "[cyan]█▀█░█▀▀░█▀▄░█▀▀░░█░░█░█░░[/]  [blue underline]https://heretic-project.org[/]"
    )
    print(
        "[cyan]▀░▀░▀▀▀░▀░▀░▀▀▀░░▀░░▀░▀▀▀[/]  [blue underline]https://github.com/p-e-w/heretic[/]"
    )
    print()


def _normalize_argv() -> None:
    standard = (
        "--collect-reproducibles" not in sys.argv and "--reproduce" not in sys.argv
    )
    positional = len(sys.argv) > 1 and not sys.argv[-1].startswith("-")
    if standard and "--model" not in sys.argv and positional:
        sys.argv.insert(-1, "--model")
    alternate = "--collect-reproducibles" in sys.argv or "--reproduce" in sys.argv
    if alternate and "--model" not in sys.argv:
        sys.argv.extend(["--model", ""])


def _read_settings() -> Settings | None:
    try:
        return Settings()  # ty:ignore[missing-argument]
    except ValidationError as error:
        print(f"[red]Configuration contains [bold]{error.error_count()}[/] errors:[/]")
        for details in error.errors():
            print(f"[bold]{details['loc'][0]}[/]: [yellow]{details['msg']}[/]")
        print()
        print("Run [bold]heretic --help[/] or see [bold]config.default.toml[/].")
        return None


def _load_reproduction(settings: Settings) -> ReproductionState | None:
    if settings.reproduce is None:
        return ReproductionState(settings, None, None, None)
    print(f"Loading reproduction information from [bold]{settings.reproduce}[/]...")
    information = load_reproduction_information(settings.reproduce)
    try:
        parameters = trial_methods.normalize_reproduction_parameters(information)
    except ValueError as error:
        print(f"[red]Invalid reproduction information: [bold]{error}[/][/]")
        return None
    if not check_environment(settings, information):
        return None
    stored = Settings.model_validate(information["settings"])
    report = workflow.load_reproduction_acceptance(
        settings.reproduce, information, stored.acceptance_gate is not None
    )
    return ReproductionState(
        merge_study_settings(stored, settings), information, parameters, report
    )


def _configure_runtime(settings: Settings) -> None:
    if settings.seed is None:
        settings.seed = random.randint(0, 2**32 - 1)
    transformers.set_seed(settings.seed)
    if settings.abliteration_method == AbliterationMethod.ARA:
        workflow.configure_runtime_determinism()
    print(get_accelerator_info())
    if settings.print_debug_information:
        print(torch.__config__.show().strip())
        print(
            f"torch.backends.mkldnn.enabled = [bold]{torch.backends.mkldnn.enabled}[/]"
        )
        print(f"torch.get_num_threads() = [bold]{torch.get_num_threads()}[/]")
        print(
            f"torch.get_num_interop_threads() = [bold]{torch.get_num_interop_threads()}[/]"
        )
    torch.set_grad_enabled(False)
    torch._dynamo.config.cache_size_limit = 64
    transformers.logging.set_verbosity_error()
    logging.getLogger("lm_eval").setLevel(logging.ERROR)
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    warnings.filterwarnings("ignore", category=ExperimentalWarning)


def _checkpoint_path(settings: Settings) -> Path:
    os.makedirs(settings.study_checkpoint_dir, exist_ok=True)
    slug = "".join(
        c if c.isalnum() or c in {"_", "-"} else "--" for c in settings.model
    )
    return Path(settings.study_checkpoint_dir) / f"{slug}.jsonl"


def _journal(path: Path) -> tuple[JournalStorage, JournalFileOpenLock]:
    lock = JournalFileOpenLock(str(path))
    backend = JournalFileBackend(str(path), lock_obj=lock)
    return JournalStorage(backend), lock


def _checkpoint_choices(existing: Any, settings: Settings) -> list[Choice]:
    finished = existing.user_attrs.get("finished", False)
    if settings.checkpoint_action is None:
        print()
        status = "already processed" if finished else "was interrupted"
        print(f"[yellow]The previous run {status}.[/] Resume it or start from scratch.")
    title = (
        "Show the results from the previous run"
        if finished
        else "Continue the previous run"
    )
    return [
        Choice(title, value="continue"),
        Choice("Ignore the previous run and start from scratch", value="restart"),
        Choice("Exit program", value=""),
    ]


def _checkpoint_action(existing: Any, settings: Settings) -> str | None:
    if settings.checkpoint_action is not None:
        return settings.checkpoint_action
    return ask_if_unset(
        None,
        questionary.select(
            "How would you like to proceed?",
            choices=_checkpoint_choices(existing, settings),
            style=Style([("highlighted", "reverse")]),
        ),
    )


def _resolve_checkpoint(
    settings: Settings, reproduction: bool
) -> CheckpointState | None:
    path = _checkpoint_path(settings)
    storage, lock = _journal(path)
    studies = storage.get_all_studies()
    existing = studies[0] if studies else None
    should_prompt = (
        existing is not None and settings.evaluate_model is None and not reproduction
    )
    if not should_prompt:
        return CheckpointState(settings, path, storage)
    assert existing is not None
    action = _checkpoint_action(existing, settings)
    if not action:
        return None
    if action == "continue":
        stored = Settings.model_validate_json(existing.user_attrs["settings"])
        settings = merge_study_settings(stored, settings)
        trial_methods.validate_study_identity(
            existing.user_attrs.get("study_fingerprint"),
            existing.user_attrs.get("study_manifest"),
            settings,
        )
    else:
        path.unlink()
        storage = JournalStorage(JournalFileBackend(str(path), lock_obj=lock))
    return CheckpointState(settings, path, storage)


def _preflight_trajectory_study(
    checkpoint: CheckpointState,
    reproduction: ReproductionState,
) -> None:
    settings = checkpoint.settings
    if (
        settings.ara_objective_version != "trajectory-v2"
        or reproduction.information is not None
    ):
        return
    study = study_runner.prepare_trajectory_study(
        study_runner.TrajectoryStudyContext(
            study_name="heretic",
            storage=checkpoint.storage,
            directions=(StudyDirection.MINIMIZE, StudyDirection.MINIMIZE),
            seed=cast(int, settings.seed),
            anchors=settings.ara_seed_trials,
            startup_trials=settings.n_startup_trials,
            required_trials=settings.n_trials,
            recover_orphans=settings.recover_orphaned_trials,
            recovery_path=checkpoint.path.with_suffix(".recovery.jsonl"),
        )
    )
    study.set_user_attr("settings", settings.model_dump_json())
    study.set_user_attr(
        "study_fingerprint", trial_methods.build_study_fingerprint(settings)
    )
    study.set_user_attr("study_manifest", trial_methods.build_study_manifest(settings))


def _protocol_roles(settings: Settings, validation: list[Any]) -> list[RoleDataset]:
    gate = settings.acceptance_gate
    assert gate is not None
    roles = [
        RoleDataset("calibration_good", settings.good_prompts),
        RoleDataset("calibration_bad", settings.bad_prompts),
        RoleDataset("audit_harmful", gate.keyword_audit_prompts),
        RoleDataset("audit_harmless", gate.kl_audit_prompts),
    ]
    for specification in validation:
        role = (
            "validation_harmful"
            if specification.dataset == settings.bad_prompts.dataset
            else "validation_harmless"
        )
        roles.append(RoleDataset(role, specification))
    return roles


def _preload_protocol(
    settings: Settings,
) -> tuple[dict[Any, list[Any]], list[Any] | None, list[Any] | None]:
    if settings.ara_objective_version != "trajectory-v2":
        return {}, None, None
    gate = settings.acceptance_gate
    assert gate is not None
    validation = configured_dataset_specs(settings.model_extra)
    roles = _protocol_roles(settings, validation)
    validate_role_sizes(
        roles,
        {
            "calibration_good": 300,
            "calibration_bad": 300,
            "validation_harmful": gate.expected_samples,
            "validation_harmless": gate.expected_samples,
            "audit_harmful": gate.expected_samples,
            "audit_harmless": gate.expected_samples,
        },
    )
    validate_role_overlaps(roles)

    def loader(item: Any) -> list[Any]:
        return load_prompts(settings, item)

    good = list(
        materialize_prompt_bundle(
            "calibration_good", settings.good_prompts, loader
        ).prompts
    )
    bad = list(
        materialize_prompt_bundle(
            "calibration_bad", settings.bad_prompts, loader
        ).prompts
    )
    cache = {
        prompt_spec_cache_key(settings.good_prompts): good,
        prompt_spec_cache_key(settings.bad_prompts): bad,
    }
    for specification in validation:
        key = prompt_spec_cache_key(specification)
        if key not in cache:
            cache[key] = list(
                materialize_prompt_bundle("validation", specification, loader).prompts
            )
    preflight_audit_metadata(gate.keyword_audit_prompts)
    preflight_audit_metadata(gate.kl_audit_prompts)
    return cache, good, bad


def _load_prompt_pair(
    settings: Settings,
    good: list[Any] | None,
    bad: list[Any] | None,
) -> tuple[list[Any], list[Any]]:
    print(f"Loading good prompts from [bold]{settings.good_prompts.dataset}[/]...")
    good_prompts = good or load_prompts(settings, settings.good_prompts)
    print(f"* [bold]{len(good_prompts)}[/] prompts loaded")
    print(f"Loading bad prompts from [bold]{settings.bad_prompts.dataset}[/]...")
    bad_prompts = bad or load_prompts(settings, settings.bad_prompts)
    print(f"* [bold]{len(bad_prompts)}[/] prompts loaded")
    return good_prompts, bad_prompts


def _select_calibration(
    settings: Settings, prompts: tuple[list[Any], list[Any]]
) -> PromptState:
    good, bad = prompts
    if settings.abliteration_method != AbliterationMethod.ARA:
        return PromptState(good, bad, {}, None)
    good, bad, manifest = workflow.select_calibration_prompts(
        good, bad, settings.ara_calibration_size, cast(int, settings.seed)
    )
    manifest = replace(
        manifest,
        capture_batch_size=settings.ara_capture_batch_size,
        good_dataset=settings.good_prompts.dataset,
        good_revision=settings.good_prompts.commit,
        bad_dataset=settings.bad_prompts.dataset,
        bad_revision=settings.bad_prompts.commit,
    )
    print(f"* Selected [bold]{len(good)}+{len(bad)}[/] CARA calibration prompts")
    return PromptState(good, bad, {}, manifest)


def _batch_performance(model: Model, prompts: list[Any]) -> float:
    model.get_responses(prompts)
    started = time.perf_counter()
    responses = model.get_responses(prompts)
    elapsed = time.perf_counter() - started
    lengths = [len(model.tokenizer.encode(response)) for response in responses]
    return sum(lengths) / elapsed


def _auto_batch_size(settings: Settings, model: Model, prompts: list[Any]) -> None:
    if settings.batch_size != 0:
        return
    print("Determining optimal batch size...")
    size, best_size, best_speed = 1, -1, -1.0
    while size <= settings.max_batch_size:
        selected = (prompts * math.ceil(size / len(prompts)))[:size]
        try:
            speed = _batch_performance(model, selected)
        except Exception as error:
            if size == 1:
                raise
            print(f"[red]Failed: {format_exception(error)}[/]")
            break
        print(f"* Batch [bold]{size}[/]: [green]{speed:.0f} tokens/s[/]")
        if speed > best_speed:
            best_size, best_speed = size, speed
        size *= 2
    settings.batch_size = best_size
    print(f"* Chosen batch size: [bold]{settings.batch_size}[/]")


def _extend_cot_prefix(settings: Settings, model: Model, prompts: list[Any]) -> None:
    prefix = settings.response_prefix
    if prefix is None:
        return
    for initializer, closed in settings.chain_of_thought_skips:
        if not prefix.startswith(initializer):
            continue
        settings.response_prefix = closed
        responses = model.get_responses_batched(prompts)
        additional = commonprefix(responses).rstrip(" ")
        if additional:
            settings.response_prefix += additional
        return


def _detect_response_prefix(
    settings: Settings,
    model: Model,
    good: list[Any],
    bad: list[Any],
) -> None:
    if settings.response_prefix is not None:
        return
    print("Checking for common response prefix...")
    prompts = good[:100] + bad[:100]
    settings.response_prefix = commonprefix(
        model.get_responses_batched(prompts)
    ).rstrip(" ")
    if not settings.response_prefix:
        print("* None found")
        return
    print(f"* Prefix found: [bold]{settings.response_prefix!r}[/]")
    _extend_cot_prefix(settings, model, prompts)


def _evaluate_only(settings: Settings, model: Model, evaluator: Evaluator) -> bool:
    if settings.evaluate_model is None:
        return False
    print(f"Loading model [bold]{settings.evaluate_model}[/]...")
    settings.model = settings.evaluate_model
    model.reset_model()
    for score_name, score in evaluator.get_scores():
        print(f"  * {score_name}: [bold]{score.rich_display}[/]")
    return True


def _prepare_pipeline_artifacts(
    checkpoint: CheckpointState,
    reproduction: ReproductionState,
    model: Model,
    prompts: PromptState,
) -> tuple[str, str | None, Any]:
    settings = checkpoint.settings
    fingerprint = trial_methods.build_study_fingerprint(settings)
    inputs = study_runner.ArtifactPreparation(
        settings,
        model,
        prompts.good,
        prompts.bad,
        prompts.calibration_manifest,
        fingerprint,
    )
    calibration, artifacts = study_runner.prepare_method_artifacts(inputs)
    workflow.validate_reproduction_artifacts(
        reproduction.information, artifacts, fingerprint
    )
    return fingerprint, calibration, artifacts


def _execute_pipeline(
    checkpoint: CheckpointState,
    reproduction: ReproductionState,
    model: Model,
    prompts: PromptState,
    evaluator: Evaluator,
) -> None:
    settings = checkpoint.settings
    fingerprint, calibration, artifacts = _prepare_pipeline_artifacts(
        checkpoint, reproduction, model, prompts
    )
    execution = study_runner.execute_study(
        study_runner.ObjectiveContext(
            settings,
            model,
            evaluator,
            artifacts,
            fingerprint,
            calibration,
            prompts.good,
        ),
        checkpoint.storage,
        checkpoint.path,
        reproduction.information is not None,
    )
    study_runner.run_trial_console(
        study_runner.TrialConsoleContext(
            settings,
            model,
            evaluator,
            artifacts,
            execution.study,
            execution.objective,
            execution.objective_names,
            reproduction.information,
            reproduction.parameters,
            fingerprint,
            calibration,
            checkpoint.path,
        )
    )


def run() -> None:
    """Execute the command-line workflow."""
    os.environ.setdefault("PYTORCH_ALLOC_CONF", "expandable_segments:True")
    _print_banner()
    _normalize_argv()
    settings = _read_settings()
    if settings is None:
        return
    if settings.collect_reproducibles is not None:
        collect_reproducibles(settings.collect_reproducibles)
        return
    reproduction = _load_reproduction(settings)
    if reproduction is None:
        return
    _configure_runtime(reproduction.settings)
    checkpoint = _resolve_checkpoint(
        reproduction.settings, reproduction.information is not None
    )
    if checkpoint is None:
        return
    settings = checkpoint.settings
    workflow.ensure_acceptance_study_unlocked(settings, str(checkpoint.path))
    _preflight_trajectory_study(checkpoint, reproduction)
    cache, preloaded_good, preloaded_bad = _preload_protocol(settings)
    model = Model(settings)
    workflow.validate_reproduction_model(model, reproduction.acceptance_report)
    print_memory_usage()
    pair = _load_prompt_pair(settings, preloaded_good, preloaded_bad)
    selected = _select_calibration(settings, pair)
    prompts = PromptState(
        selected.good, selected.bad, cache, selected.calibration_manifest
    )
    _auto_batch_size(settings, model, prompts.good)
    _detect_response_prefix(settings, model, prompts.good, prompts.bad)
    evaluator = Evaluator(settings, model, prompt_cache=cache)
    if _evaluate_only(settings, model, evaluator):
        return
    if reproduction.information is None and not evaluator.get_objective_names():
        print("[red]No optimization objectives configured.[/]")
        return
    _execute_pipeline(checkpoint, reproduction, model, prompts, evaluator)


def main():
    # Install Rich traceback handler.
    install()

    try:
        run()
    except BaseException as error:
        # Transformers appears to handle KeyboardInterrupt (or BaseException)
        # internally in some places, which can re-raise a different error in the handler,
        # masking the root cause. We therefore check both the error itself and its context.
        if isinstance(error, KeyboardInterrupt) or isinstance(
            error.__context__, KeyboardInterrupt
        ):
            print()
            print("[red]Shutting down...[/]")
        else:
            raise
