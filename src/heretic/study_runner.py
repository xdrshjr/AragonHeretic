# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single-worker, resume-safe Optuna runner for trajectory-v2."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, cast

import optuna
import questionary
import torch
from optuna import Study, Trial
from optuna.exceptions import TrialPruned
from optuna.samplers import RandomSampler, TPESampler
from optuna.storages import BaseStorage
from optuna.study import StudyDirection
from optuna.trial import FrozenTrial, TrialState
from questionary import Choice, Style

from . import (
    acceptance,
    acceptance_export,
    ara,
    ara_runtime,
    ara_search,
    ara_trajectory,
    trial_methods,
)
from .artifact_schema import atomic_write_json
from .ara_config import ARASeedTrial
from .ara_search import (
    constraints_from_trial,
    derive_sampler_seed,
    enqueue_anchor_prefix,
    recover_orphaned_trials,
    validate_trial_envelope,
)
from .config import AbliterationMethod, Settings
from .system import empty_cache
from .utils import (
    Prompt,
    ask_if_unset,
    format_duration,
    get_trial_parameters,
    print,
    print_memory_usage,
)


@dataclass(frozen=True)
class TrajectoryStudyContext:
    study_name: str
    storage: optuna.storages.BaseStorage
    directions: Sequence[StudyDirection]
    seed: int
    anchors: Sequence[ARASeedTrial]
    startup_trials: int = 24
    required_trials: int = 120
    recover_orphans: bool = False
    recovery_path: Path | None = None


@dataclass(frozen=True)
class ArtifactPreparation:
    settings: Settings
    model: Any
    good_prompts: list[Prompt]
    bad_prompts: list[Prompt]
    calibration_manifest: Any
    study_fingerprint: str


@dataclass(frozen=True)
class ObjectiveContext:
    settings: Settings
    model: Any
    evaluator: Any
    artifacts: Any
    study_fingerprint: str
    calibration_fingerprint: str | None
    good_prompts: list[Prompt]


@dataclass(frozen=True)
class StudyExecution:
    study: Study | None
    objective: Callable[[Trial], tuple[float, ...]]
    objective_names: list[str]


@dataclass(frozen=True)
class TrialConsoleContext:
    """All state required after study execution and before process exit."""

    settings: Settings
    model: Any
    evaluator: Any
    artifacts: Any
    study: Study | None
    objective: Callable[[Trial], tuple[float, ...]]
    objective_names: list[str]
    reproduction: Mapping[str, Any] | None
    reproduction_parameters: Mapping[str, Any] | None
    study_fingerprint: str
    calibration_fingerprint: str | None
    checkpoint_path: Path


class TrialObjective:
    def __init__(self, context: ObjectiveContext) -> None:
        self.context = context
        self.trial_index = 0
        self.start_index = 0
        self.start_time = time.perf_counter()

    def resume_at(self, index: int) -> None:
        """Resume console progress accounting at a completed attempt index."""
        self.trial_index = index
        self.start_index = index

    def _set_trial_identity(self, trial: Trial) -> Any:
        context = self.context
        method_context = trial_methods.MethodContext(
            settings=context.settings,
            layer_count=len(context.model.get_layers()),
            components=tuple(context.model.get_abliterable_components()),
        )
        parameters = trial_methods.sample_method_parameters(trial, method_context)
        trial.set_user_attr("model_fingerprint", context.model.model_fingerprint)
        trial.set_user_attr("study_fingerprint", context.study_fingerprint)
        if context.calibration_fingerprint is not None:
            trial.set_user_attr(
                "calibration_fingerprint", context.calibration_fingerprint
            )
        if isinstance(context.artifacts, trial_methods.TrajectoryArtifacts):
            trial.set_user_attr(
                "trajectory_fingerprint", context.artifacts.trajectory_fingerprint
            )
        return parameters

    def _print_trial(self, trial: Trial) -> None:
        settings = self.context.settings
        print()
        print(
            f"Running trial [bold]{self.trial_index}[/] of "
            f"[bold]{settings.n_trials}[/]..."
        )
        print("* Parameters:")
        for name, value in get_trial_parameters(trial).items():
            print(f"  * {name} = [bold]{value}[/]")

    def _evaluate(self, trial: Trial, parameters: Any) -> tuple[float, ...]:
        context = self.context
        print("* Abliterating...")
        try:
            summary = trial_methods.apply_trial(
                context.model, parameters, context.artifacts
            )
            if summary.ara is not None:
                trial.set_user_attr("ara_summary", asdict(summary.ara))
            print("* Evaluating...")
            scores = context.evaluator.get_scores()
            context.model._validate_ara_runtime()
        finally:
            trial_methods.cleanup_trial(context.model, context.artifacts)
        for name, score in scores:
            print(f"    * {name}: [bold]{score.rich_display}[/]")
        records = context.evaluator.get_paired_score_records(scores)
        trial.set_user_attr("scores", records)
        if context.settings.ara_objective_version == "trajectory-v2":
            trial.set_user_attr("score_records", records)
            ara_search.set_candidate_constraints(
                trial, records, cast(Any, context.settings.acceptance_gate)
            )
        return context.evaluator.get_objective_values(scores)

    def _print_timing(self) -> None:
        settings = self.context.settings
        elapsed = time.perf_counter() - self.start_time
        completed = self.trial_index - self.start_index
        remaining = (elapsed / completed) * (settings.n_trials - self.trial_index)
        print()
        print(f"[grey50]Elapsed time: [bold]{format_duration(elapsed)}[/][/]")
        if self.trial_index < settings.n_trials:
            print(
                "[grey50]Estimated remaining time: "
                f"[bold]{format_duration(remaining)}[/][/]"
            )
        print_memory_usage()

    def _record_v2_failure(
        self, trial: Trial, category: str, stage: str, error: BaseException
    ) -> None:
        if self.context.settings.ara_objective_version != "trajectory-v2":
            return
        key = getattr(error, "key", None)
        record = ara_search.failure_record(
            trial.number, category, stage, error, None if key is None else str(key)
        )
        trial.set_user_attr("failure_record", asdict(record))

    def _handle_oom(self, trial: Trial, error: torch.OutOfMemoryError) -> None:
        context = self.context
        trial.set_user_attr("failure", trial_methods.failure_record(error, "oom"))
        self._record_v2_failure(trial, "oom", "trial", error)
        trial_methods.cleanup_trial(context.model, context.artifacts)
        empty_cache()
        try:
            context.model.generate(
                context.good_prompts[:1], max_new_tokens=1, use_cache=False
            )
        except Exception:
            raise error

    def __call__(self, trial: Trial) -> tuple[float, ...]:
        if self.trial_index == self.start_index:
            self.start_index = trial.number
        self.trial_index = trial.number + 1
        trial.set_user_attr("index", self.trial_index)
        try:
            parameters = self._set_trial_identity(trial)
            self._print_trial(trial)
            values = self._evaluate(trial, parameters)
            self._print_timing()
            return values
        except ara.ARAOptimizationError as error:
            trial.set_user_attr("failure", trial_methods.failure_record(error, "ara"))
            category = getattr(error, "category", "optimization_guard")
            if isinstance(error, ara_trajectory.TrajectoryNonFiniteError):
                category = "non_finite"
            self._record_v2_failure(trial, category, error.stage, error)
            raise TrialPruned() from error
        except torch.OutOfMemoryError as error:
            self._handle_oom(trial, error)
            raise TrialPruned() from error
        except KeyboardInterrupt as error:
            self._record_v2_failure(trial, "interrupted", "trial", error)
            trial.study.stop()
            raise
        except BaseException as error:
            trial.set_user_attr("failure", trial_methods.failure_record(error, "trial"))
            self._record_v2_failure(trial, "unknown", "trial", error)
            raise


def _capture_guard(
    settings: Settings,
    model: Any,
    prompt_count: int,
) -> None:
    guard = settings.ara_runtime_guard
    if settings.ara_objective_version != "trajectory-v2" or guard is None:
        return
    capture_gib = ara_trajectory.estimate_capture_gib(
        model.ara_targets, prompt_count, settings.ara_trajectory_tokens
    )
    if capture_gib > guard.max_capture_cpu_gib:
        raise RuntimeError(
            f"estimated trajectory capture {capture_gib:.2f} GiB exceeds "
            "the configured CPU guard"
        )
    print(f"* Estimated trajectory bank: [bold]{capture_gib:.2f} GiB[/]")


def _trajectory_artifacts(inputs: ArtifactPreparation) -> Any:
    settings, model = inputs.settings, inputs.model
    good_io, good_continuations = model.capture_ara_trajectory_io(inputs.good_prompts)
    print("* Capturing bad prompts...")
    bad_io, bad_continuations = model.capture_ara_trajectory_io(inputs.bad_prompts)
    bank = ara_trajectory.build_trajectory_bank(good_io, bad_io)
    model._validate_ara_runtime()
    manifest = ara_trajectory.build_trajectory_manifest(
        inputs, good_continuations, bad_continuations
    )
    optimizer = ara_trajectory.TrajectoryOptimizerConfig(
        max_iter=settings.ara_lbfgs_max_iter,
        history_size=settings.ara_lbfgs_history_size,
        temperature=settings.ara_softmin_temperature,
        max_good_delta_rms=settings.ara_max_good_delta_rms,
        max_singular_value=settings.ara_max_singular_value,
    )
    return trial_methods.TrajectoryArtifacts(
        calibration_bank=bank,
        adapter_initial_state=model.adapter_initial_state,
        targets=model.ara_targets,
        optimizer_config=optimizer,
        trajectory_fingerprint=ara_trajectory.canonical_manifest_sha256(manifest),
        trajectory_manifest=manifest,
    )


def _point_artifacts(inputs: ArtifactPreparation) -> ara.ARAArtifacts:
    settings, model = inputs.settings, inputs.model
    good_io = model.capture_ara_module_io(inputs.good_prompts)
    print("* Capturing bad prompts...")
    bad_io = model.capture_ara_module_io(inputs.bad_prompts)
    bank = ara.build_calibration_bank(good_io, bad_io)
    optimizer = ara.ARAOptimizerConfig(
        max_iter=settings.ara_lbfgs_max_iter,
        history_size=settings.ara_lbfgs_history_size,
        temperature=settings.ara_softmin_temperature,
    )
    return ara.ARAArtifacts(
        calibration_bank=bank,
        adapter_initial_state=model.adapter_initial_state,
        calibration_manifest=inputs.calibration_manifest,
        model_fingerprint=model.model_fingerprint,
        study_fingerprint=inputs.study_fingerprint,
        targets=model.ara_targets,
        optimizer_config=optimizer,
    )


def prepare_method_artifacts(
    inputs: ArtifactPreparation,
) -> tuple[str | None, Any]:
    """Prepare immutable directional or trajectory artifacts before optimization."""
    settings, model = inputs.settings, inputs.model
    if settings.abliteration_method != AbliterationMethod.ARA:
        from .workflow import prepare_directional_artifacts

        artifacts = prepare_directional_artifacts(
            settings, model, inputs.good_prompts, inputs.bad_prompts
        )
        return None, artifacts
    assert inputs.calibration_manifest is not None
    assert model.adapter_initial_state is not None
    print()
    print("Capturing clean CARA module I/O...")
    _capture_guard(settings, model, len(inputs.good_prompts))
    print("* Capturing good prompts...")
    if settings.ara_objective_version == "trajectory-v2":
        artifacts = _trajectory_artifacts(inputs)
    else:
        artifacts = _point_artifacts(inputs)
    fingerprint = trial_methods.canonical_fingerprint(
        asdict(inputs.calibration_manifest)
    )
    empty_cache()
    return fingerprint, artifacts


def _phase(number: int, anchor_count: int, startup_trials: int) -> str:
    if number < anchor_count:
        return "anchor"
    if number < anchor_count + startup_trials:
        return "startup"
    return "tpe"


def _sampler(
    context: TrajectoryStudyContext, number: int
) -> optuna.samplers.BaseSampler:
    phase = _phase(number, len(context.anchors), context.startup_trials)
    seed = derive_sampler_seed(context.seed, phase, number)
    if phase != "tpe":
        return RandomSampler(seed=seed)
    return TPESampler(
        seed=seed,
        n_startup_trials=0,
        multivariate=True,
        constraints_func=constraints_from_trial,
    )


def _load_study(
    context: TrajectoryStudyContext,
    number: int,
) -> Study:
    return optuna.load_study(
        study_name=context.study_name,
        storage=context.storage,
        sampler=_sampler(context, number),
    )


def _next_attempt(study: Study, required_trials: int) -> int:
    trials = sorted(study.get_trials(deepcopy=False), key=lambda trial: trial.number)
    validate_trial_envelope(trials, required_trials)
    active = [
        trial
        for trial in trials
        if trial.state in {TrialState.WAITING, TrialState.RUNNING}
    ]
    if active:
        return active[0].number
    return len(trials)


def prepare_trajectory_study(context: TrajectoryStudyContext) -> Study:
    """Create/load a study, recover proven orphans, and repair anchor suffixes."""
    if len(context.anchors) != 8:
        raise ValueError("trajectory-v2 requires exactly eight anchors")
    if context.startup_trials != 24 or context.required_trials != 120:
        raise ValueError("trajectory-v2 protocol must use 8 + 24 + 88 attempts")
    study = optuna.create_study(
        study_name=context.study_name,
        storage=context.storage,
        directions=list(context.directions),
        load_if_exists=True,
    )
    recover_orphaned_trials(
        study,
        context.recover_orphans,
        context.recovery_path,
    )
    enqueue_anchor_prefix(study, context.anchors)
    return study


def run_trajectory_study(
    context: TrajectoryStudyContext,
    objective: Callable[[Trial], tuple[float, ...]],
) -> Study:
    """Run exactly one derived-sampler attempt at a time through trial 119."""
    study = prepare_trajectory_study(context)
    while True:
        number = _next_attempt(study, context.required_trials)
        if number == context.required_trials:
            break
        phase = _phase(number, len(context.anchors), context.startup_trials)
        attempt_study = _load_study(context, number)

        def wrapped(trial: Trial) -> tuple[float, ...]:
            if trial.number != number:
                raise RuntimeError(
                    f"attempt number {trial.number} does not match expected {number}"
                )
            trial.set_user_attr("attempt_number", number)
            trial.set_user_attr("protocol_phase", phase)
            if phase == "anchor":
                trial.set_user_attr("anchor_index", number)
            return objective(trial)

        attempt_study.optimize(wrapped, n_trials=1)
        study = _load_study(context, number)
    trials = sorted(study.get_trials(deepcopy=False), key=lambda trial: trial.number)
    validate_trial_envelope(trials, context.required_trials, require_terminal=True)
    return study


def _create_study(
    context: ObjectiveContext,
    storage: BaseStorage,
) -> Study:
    settings = context.settings
    directions = context.evaluator.get_objective_directions()
    if settings.ara_objective_version == "trajectory-v2":
        return optuna.create_study(
            storage=storage,
            directions=directions,
            study_name="heretic",
            load_if_exists=True,
        )
    return optuna.create_study(
        sampler=TPESampler(
            n_startup_trials=settings.n_startup_trials,
            n_ei_candidates=128,
            multivariate=True,
            seed=settings.seed,
            constraints_func=trial_methods.failure_constraint,
        ),
        storage=storage,
        directions=directions,
        study_name="heretic",
        load_if_exists=True,
    )


def _run_registered_trajectory(
    study_context: ObjectiveContext,
    storage: BaseStorage,
    checkpoint_path: Path,
    objective: TrialObjective,
) -> Study:
    settings = study_context.settings
    return run_trajectory_study(
        TrajectoryStudyContext(
            study_name="heretic",
            storage=storage,
            directions=study_context.evaluator.get_objective_directions(),
            seed=cast(int, settings.seed),
            anchors=settings.ara_seed_trials,
            startup_trials=settings.n_startup_trials,
            required_trials=settings.n_trials,
            recover_orphans=settings.recover_orphaned_trials,
            recovery_path=checkpoint_path.with_suffix(".recovery.jsonl"),
        ),
        objective,
    )


def execute_study(
    context: ObjectiveContext,
    storage: BaseStorage,
    checkpoint_path: Path,
    reproduction_mode: bool,
) -> StudyExecution:
    """Create/resume and execute the configured point-v1 or trajectory-v2 study."""
    objective = TrialObjective(context)
    objective_names = context.evaluator.get_objective_names()
    if reproduction_mode:
        return StudyExecution(None, objective, objective_names)
    settings = context.settings
    study = _create_study(context, storage)
    study.set_user_attr("settings", settings.model_dump_json())
    ara_search.bind_study_identity(study, context.study_fingerprint, context.artifacts)
    study.set_user_attr("study_manifest", trial_methods.build_study_manifest(settings))
    study.set_user_attr("finished", False)
    objective.resume_at(len(study.trials))
    if study.trials:
        print()
        print("Resuming existing study.")
    try:
        if settings.ara_objective_version == "trajectory-v2":
            study = _run_registered_trajectory(
                context, storage, checkpoint_path, objective
            )
        else:
            study.optimize(objective, n_trials=settings.n_trials - len(study.trials))
    except KeyboardInterrupt:
        pass
    if len(study.trials) == settings.n_trials:
        study.set_user_attr("finished", True)
    return StudyExecution(study, objective, objective_names)


def _trial_title(trial: FrozenTrial) -> str:
    parts = [
        f"{score['name']}: {score['score']['rich_display']}"
        for score in trial.user_attrs["scores"]
    ]
    return f"[Trial {trial.user_attrs['index']:>3}] " + ", ".join(parts)


def _pareto_choices(
    study: Study,
    names: Sequence[str],
) -> tuple[list[FrozenTrial], list[Choice]]:
    trials = sorted(
        study.best_trials,
        key=lambda trial: tuple(
            next(
                (
                    score["score"]["value"]
                    for score in trial.user_attrs["scores"]
                    if score["name"] == name
                ),
                None,
            )
            for name in names
        ),
    )
    choices = [Choice(_trial_title(trial), value=trial) for trial in trials]
    choices += [
        Choice("Run additional trials", value="continue"),
        Choice("Exit program", value=""),
    ]
    return trials, choices


def _candidate_failure(
    context: TrialConsoleContext,
    error: BaseException,
) -> None:
    gate = cast(Any, context.settings.acceptance_gate)
    study = cast(Study, context.study)
    if context.settings.ara_objective_version == "trajectory-v2":
        payload = acceptance.failed_acceptance_payload(
            study, gate, context.study_fingerprint, "validation-selection"
        )
        payload["failure_reason"] = trial_methods.safe_failure_reason(error)
        atomic_write_json(gate.report_path, payload)
        return
    trial_methods.write_acceptance_report(
        gate.report_path,
        trial_methods.AcceptanceReport(
            status="failed",
            reason=trial_methods.safe_failure_reason(error),
            model_fingerprint=context.model.model_fingerprint,
            study_fingerprint=context.study_fingerprint,
            calibration_fingerprint=context.calibration_fingerprint,
        ),
    )


def _automatic_candidate(context: TrialConsoleContext) -> FrozenTrial | None:
    gate = context.settings.acceptance_gate
    if gate is None:
        return None
    study = cast(Study, context.study)
    try:
        if context.settings.ara_objective_version == "trajectory-v2":
            selected = acceptance.select_for_acceptance(
                study, gate, context.study_fingerprint
            )
        else:
            from .workflow import select_for_acceptance

            selected = select_for_acceptance(
                study.trials,
                gate,
                context.study_fingerprint,
                context.settings.model,
            )
    except (trial_methods.AcceptanceGateError, acceptance.AcceptanceGateError) as error:
        _candidate_failure(context, error)
        raise
    selected.user_attrs["failure_trials"] = trial_methods.collect_failure_records(
        study.trials
    )
    path = context.checkpoint_path.with_suffix(".selection.jsonl")
    trial_methods.append_selection_record(path, selected, context.study_fingerprint)
    return selected


def _choose_trial(
    context: TrialConsoleContext,
    automatic: FrozenTrial | None,
    trials: list[FrozenTrial],
    choices: list[Choice],
) -> FrozenTrial | str | None:
    if context.reproduction is not None:
        from .workflow import make_reproduction_trial

        return make_reproduction_trial(
            context.reproduction,
            cast(Mapping[str, Any], context.reproduction_parameters),
        )
    index = context.settings.trial_index
    default = (
        automatic
        if automatic is not None
        else (None if index is None else trials[index])
    )
    return ask_if_unset(
        default,
        questionary.select(
            "Which trial do you want to use?",
            choices=choices,
            style=Style([("highlighted", "reverse")]),
        ),
    )


def _replay_candidate(
    context: TrialConsoleContext,
    trial: FrozenTrial,
) -> tuple[list[dict[str, Any]] | None, acceptance.ReplayEvidence | None]:
    gate = context.settings.acceptance_gate
    if gate is None:
        return None, None
    try:
        if context.settings.ara_objective_version != "trajectory-v2":
            from .workflow import AcceptanceRuntime, run_acceptance_gate

            runtime = AcceptanceRuntime(
                context.settings, context.model, context.artifacts, context.evaluator
            )
            return run_acceptance_gate(runtime, trial), None
        replay = acceptance.replay_candidate(
            acceptance.ReplayContext(
                apply=lambda item: trial_methods.apply_trial(
                    context.model,
                    trial_methods.parameters_from_trial(item),
                    context.artifacts,
                ),
                score=lambda: context.evaluator.get_paired_score_records(
                    context.evaluator.get_scores()
                ),
                capture_state=lambda: ara_runtime.capture_adapter_factor_state(
                    context.model.ara_targets
                ),
                cleanup=lambda: trial_methods.cleanup_trial(
                    context.model, context.artifacts
                ),
                gate=gate,
            ),
            trial,
        )
        return None, replay
    except BaseException as error:
        _replay_failure(context, trial, error)
        raise


def _replay_failure(
    context: TrialConsoleContext,
    trial: FrozenTrial,
    error: BaseException,
) -> None:
    trial_methods.cleanup_trial(context.model, context.artifacts)
    gate = cast(Any, context.settings.acceptance_gate)
    source = context.study if context.study is not None else [trial]
    if context.settings.ara_objective_version == "trajectory-v2":
        report = acceptance.failed_acceptance_payload(
            source, gate, context.study_fingerprint, "validation-replay"
        )
        report["selected_trial_number"] = trial.number
        report["failure_reason"] = trial_methods.safe_failure_reason(error)
        atomic_write_json(gate.report_path, report)
        return
    trial_methods.write_acceptance_report(
        gate.report_path,
        trial_methods.AcceptanceReport(
            status="failed",
            reason=trial_methods.safe_failure_reason(error),
            model_fingerprint=context.model.model_fingerprint,
            study_fingerprint=context.study_fingerprint,
            calibration_fingerprint=context.calibration_fingerprint,
            selected_trial_number=trial.number,
            parameters=trial_methods.parameter_envelope(
                trial_methods.parameters_from_trial(trial)
            ),
        ),
    )


def _selection_state(
    context: TrialConsoleContext,
) -> tuple[FrozenTrial | None, list[FrozenTrial], list[Choice]]:
    study = cast(Study, context.study)
    automatic = None
    if context.settings.acceptance_gate is not None:
        automatic = _automatic_candidate(context)
    if not any(item.state == TrialState.COMPLETE for item in study.trials):
        raise KeyboardInterrupt
    trials, choices = _pareto_choices(study, context.objective_names)
    print()
    print("[bold green]Optimization finished![/]")
    return automatic, trials, choices


def _action_context(
    context: TrialConsoleContext,
    trial: FrozenTrial,
    audit_records: list[dict[str, Any]] | None,
    replay: acceptance.ReplayEvidence | None,
) -> acceptance_export.SaveActionContext:
    def reset_model() -> None:
        print("* Resetting model...")
        print("* Abliterating...")
        trial_methods.apply_trial(
            context.model,
            trial_methods.parameters_from_trial(trial),
            context.artifacts,
        )

    return acceptance_export.SaveActionContext(
        settings=context.settings,
        model=context.model,
        evaluator=context.evaluator,
        artifacts=context.artifacts,
        study=context.study,
        trial=trial,
        replay=replay,
        audit_records=audit_records,
        study_fingerprint=context.study_fingerprint,
        calibration_fingerprint=context.calibration_fingerprint,
        checkpoint_path=context.checkpoint_path,
        reproduction=context.reproduction,
        reset_model=reset_model,
    )


def run_trial_console(context: TrialConsoleContext) -> None:
    """Run interactive or configured trial selection and export actions."""
    while True:
        if context.reproduction is None:
            automatic, trials, choices = _selection_state(context)
        else:
            automatic, trials, choices = None, [], []
        trial = _choose_trial(context, automatic, trials, choices)
        if trial in (None, ""):
            return
        if trial == "continue":
            from .workflow import run_additional_trials

            run_additional_trials(
                context.settings, cast(Study, context.study), context.objective
            )
            continue
        selected = cast(FrozenTrial, trial)
        print()
        print(f"Restoring model from trial [bold]{selected.user_attrs['index']}[/]...")
        action_context = _action_context(context, selected, None, None)
        action_context.reset_model()
        audit_records, replay = _replay_candidate(context, selected)
        action_context = _action_context(context, selected, audit_records, replay)
        acceptance_export.run_model_actions(action_context)
        if (
            context.reproduction is not None
            or context.settings.trial_index is not None
            or automatic is not None
            or context.settings.model_action is not None
        ):
            return
