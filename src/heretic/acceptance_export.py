# SPDX-License-Identifier: AGPL-3.0-or-later
"""Staged trajectory-v2 export, audit ledger, and acyclic hashes."""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence, cast

import torch
import questionary
from optuna.trial import FrozenTrial
from questionary import Choice, Style
from torch import Tensor

from .acceptance import (
    AcceptanceGateError,
    ReplayEvidence,
    validate_gate_records,
)
from .ara_config import AcceptanceGate
from .ara_search import SAMPLER_PROTOCOL, SEARCH_SPACE_VERSION
from .artifact_schema import (
    ACCEPTANCE_SCHEMA,
    REPRODUCE_SCHEMA,
    atomic_write_json,
    canonical_sha256,
    core_artifact_hashes,
    file_sha256,
    verify_artifact_graph,
)
from .config import ExportStrategy
from .system import empty_cache
from .utils import ask_if_unset, format_exception, print as rich_print

LEDGER_TRANSITIONS = {
    "not_started": {"started"},
    "started": {"consumed"},
    "consumed": {"passed", "failed"},
    "passed": set(),
    "failed": set(),
}


@dataclass(frozen=True)
class ExportIdentities:
    """Fingerprints bound into acceptance and reproduce artifacts."""

    model_fingerprint: str
    study_fingerprint: str
    data_fingerprint: str
    trajectory_fingerprint: str
    prefix_set_sha256: str


@dataclass(frozen=True)
class ExportContext:
    """Callbacks and paths required by the staged publication protocol."""

    destination: Path
    evidence_directory: Path
    gate: AcceptanceGate
    identities: ExportIdentities
    third_apply: Callable[[FrozenTrial], None]
    capture_state: Callable[[], Mapping[str, Tensor]]
    cleanup: Callable[[], None]
    write_core: Callable[[Path], Sequence[str]]
    smoke: Callable[[Path], None]
    audit: Callable[[Path], Sequence[Mapping[str, Any]]]
    study_summary: Mapping[str, Any]
    runtime_guard_report: Mapping[str, Any]
    reproduction_metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HereticExportInputs:
    """Heretic runtime objects adapted to the generic staged exporter."""

    destination: Path
    evidence_directory: Path
    settings: Any
    model: Any
    artifacts: Any
    trial: FrozenTrial
    replay: ReplayEvidence
    study_summary: Mapping[str, Any]
    checkpoint_path: Path


@dataclass(frozen=True)
class ExportResult:
    """Paths and immutable report bytes from a successful publication."""

    destination: Path
    acceptance_bytes: bytes
    reproduce_bytes: bytes


@dataclass(frozen=True)
class PublicationEvidence:
    core_hashes: Mapping[str, str]
    audit_records: Sequence[Mapping[str, Any]]
    ledger_hash: str


@dataclass(frozen=True)
class SaveActionContext:
    """Runtime state needed by the local-save model action."""

    settings: Any
    model: Any
    evaluator: Any
    artifacts: Any
    study: Any
    trial: FrozenTrial
    replay: ReplayEvidence | None
    audit_records: Sequence[Mapping[str, Any]] | None
    study_fingerprint: str
    calibration_fingerprint: str | None
    checkpoint_path: Path
    reproduction: Mapping[str, Any] | None
    reset_model: Callable[[], None]


def _read_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": "cara-audit-ledger-v1", "status": "not_started"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "cara-audit-ledger-v1":
        raise AcceptanceGateError("audit ledger schema is invalid")
    if payload.get("status") not in LEDGER_TRANSITIONS:
        raise AcceptanceGateError("audit ledger status is invalid")
    return payload


def transition_audit_ledger(
    path: str | Path,
    status: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically perform one monotonic audit-ledger transition."""
    output = Path(path)
    current = _read_ledger(output)
    if status not in LEDGER_TRANSITIONS[current["status"]]:
        raise AcceptanceGateError(
            f"invalid audit transition {current['status']} -> {status}"
        )
    payload = {
        **current,
        "status": status,
        "audit_consumed": status in {"consumed", "passed"}
        or (status == "failed" and bool(current.get("audit_consumed"))),
        "updated_ns": time.time_ns(),
    }
    if details:
        payload["details"] = dict(details)
    atomic_write_json(output, payload)
    return payload


def ensure_audit_available(path: str | Path) -> None:
    """Fail closed after any prior audit consumption or terminal ledger state."""
    ledger = _read_ledger(Path(path))
    retryable = ledger["status"] == "started" and not ledger.get(
        "audit_consumed", False
    )
    if ledger["status"] != "not_started" and not retryable:
        raise AcceptanceGateError(f"audit session cannot start from {ledger['status']}")


def _compare_export_state(
    expected: Mapping[str, Tensor],
    actual: Mapping[str, Tensor],
) -> None:
    if expected.keys() != actual.keys():
        raise AcceptanceGateError("third apply adapter state keys differ")
    for name in expected:
        if not torch.allclose(
            expected[name].to(torch.float32),
            actual[name].to(torch.float32),
            rtol=1e-6,
            atol=1e-7,
        ):
            raise AcceptanceGateError(f"third apply differs from replay: {name}")


def _passed_acceptance(
    context: ExportContext,
    trial: FrozenTrial,
    replay: ReplayEvidence,
    audit_records: Sequence[Mapping[str, Any]],
    hashes: tuple[Mapping[str, str], str],
) -> dict[str, Any]:
    core_hashes, ledger_hash = hashes
    audit_values = validate_gate_records(audit_records, context.gate)
    if (
        audit_values.keyword > context.gate.keyword_max
        or audit_values.relative_keyword_drop < context.gate.keyword_drop_min
        or audit_values.kl > context.gate.kl_max
    ):
        raise AcceptanceGateError("the unique audit did not pass its thresholds")
    return {
        "schema": ACCEPTANCE_SCHEMA,
        "status": "passed",
        "model_fingerprint": context.identities.model_fingerprint,
        "study_fingerprint": context.identities.study_fingerprint,
        "data_fingerprint": context.identities.data_fingerprint,
        "trajectory_fingerprint": context.identities.trajectory_fingerprint,
        "selected_trial_number": trial.number,
        "validation_replays": {
            "first": list(replay.first.score_records),
            "second": list(replay.second.score_records),
            "max_absolute_error": replay.max_absolute_error,
            "max_relative_error": replay.max_relative_error,
            "kl_drift": replay.kl_drift,
        },
        "audit": list(audit_records),
        "audit_consumed": True,
        "audit_ledger_sha256": ledger_hash,
        "runtime_guard_report": dict(context.runtime_guard_report),
        "study_summary": dict(context.study_summary),
        "core_artifact_hashes": dict(core_hashes),
    }


def _reproduce_payload(
    context: ExportContext,
    trial: FrozenTrial,
    core_hashes: Mapping[str, str],
    acceptance_hash: str,
) -> dict[str, Any]:
    payload = dict(context.reproduction_metadata)
    payload.update(
        {
            "schema": REPRODUCE_SCHEMA,
            "objective_version": "trajectory-v2",
            "trajectory_manifest_sha256": context.identities.trajectory_fingerprint,
            "prefix_set_sha256": context.identities.prefix_set_sha256,
            "search_space_version": SEARCH_SPACE_VERSION,
            "sampler_protocol": SAMPLER_PROTOCOL,
            "core_artifact_hashes": dict(core_hashes),
            "acceptance_sha256": acceptance_hash,
            "model_fingerprint": context.identities.model_fingerprint,
            "study_fingerprint": context.identities.study_fingerprint,
            "data_fingerprint": context.identities.data_fingerprint,
            "selected_trial_number": trial.number,
            "parameters": trial.user_attrs["ara_parameters"],
        }
    )
    payload["hashes"] = {
        name: value
        for name, value in core_hashes.items()
        if name.endswith((".safetensors", ".bin"))
    }
    payload["acceptance"] = {
        "status": "passed",
        "sha256": acceptance_hash,
        "path": "acceptance.json",
    }
    return payload


def _new_staging(destination: Path, resume: bool = False) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise AcceptanceGateError("formal adapter destination already exists")
    existing = list(destination.parent.glob(f".{destination.name}.staging-*"))
    if resume:
        if len(existing) != 1 or not existing[0].is_dir():
            raise AcceptanceGateError("retryable audit has no unique staging directory")
        return existing[0]
    if existing:
        raise AcceptanceGateError("a stale staging directory already exists")
    staging = destination.parent / f".{destination.name}.staging-{uuid.uuid4().hex}"
    staging.mkdir()
    return staging


def _prefix_fingerprint(settings: Any) -> str:
    scorer_settings = (settings.model_extra or {}).get("scorer", {})
    refusal = (
        scorer_settings.get("RefusalLogOdds", {})
        if isinstance(scorer_settings, dict)
        else {}
    )
    return canonical_sha256(
        {
            "refusal_prefixes": refusal.get("refusal_prefixes"),
            "answer_prefixes": refusal.get("answer_prefixes"),
        }
    )


def _validation_data_fingerprint(
    replay: ReplayEvidence,
    gate: AcceptanceGate,
) -> str:
    fingerprints = {}
    for record in replay.second.score_records:
        if record.get("name") not in {gate.keyword_score, gate.kl_score}:
            continue
        score = record.get("score", {})
        fingerprints[str(record["name"])] = score.get("dataset_fingerprint")
    if len(fingerprints) != 2 or any(not value for value in fingerprints.values()):
        raise AcceptanceGateError("validation data fingerprints are incomplete")
    return canonical_sha256(fingerprints)


def _core_writer(
    inputs: HereticExportInputs, parameters: Any
) -> Callable[[Path], Sequence[str]]:
    from .trial_methods import parameter_envelope
    from .utils import generate_config_toml, get_readme_intro

    def write(staging: Path) -> Sequence[str]:
        inputs.model.model.save_pretrained(
            staging, max_shard_size=inputs.settings.max_shard_size
        )
        inputs.model.tokenizer.save_pretrained(staging)
        if inputs.model.processor is not None:
            inputs.model.processor.save_pretrained(staging)
        (staging / "effective-config.toml").write_text(
            generate_config_toml(inputs.settings), encoding="utf-8"
        )
        atomic_write_json(
            staging / "trajectory-manifest.json",
            dict(inputs.artifacts.trajectory_manifest),
        )
        atomic_write_json(
            staging / "study-identity.json",
            {
                "study_fingerprint": inputs.trial.user_attrs["study_fingerprint"],
                "trial_number": inputs.trial.number,
                "parameters": parameter_envelope(parameters),
            },
        )
        readme_trial = cast(
            FrozenTrial,
            SimpleNamespace(
                user_attrs={**inputs.trial.user_attrs, "acceptance_status": "passed"}
            ),
        )
        (staging / "README.md").write_text(
            get_readme_intro(inputs.settings, readme_trial, True), encoding="utf-8"
        )
        shutil.copy2(inputs.checkpoint_path, staging / "study.jsonl")
        return [
            path.relative_to(staging).as_posix()
            for path in staging.rglob("*")
            if path.is_file()
        ]

    return write


def _audit_callback(
    inputs: HereticExportInputs,
    ledger_path: Path,
) -> Callable[[Path], Sequence[Mapping[str, Any]]]:
    def audit(staging: Path) -> Sequence[Mapping[str, Any]]:
        from .ara_runtime import adapter_state_identity
        from .workflow import verify_reloaded_adapter

        return verify_reloaded_adapter(
            inputs.settings,
            staging,
            inputs.model.model_fingerprint,
            ledger_path,
            adapter_state_identity(inputs.replay.second.adapter_state),
        )

    return audit


def _export_identities(inputs: HereticExportInputs) -> ExportIdentities:
    return ExportIdentities(
        model_fingerprint=inputs.model.model_fingerprint,
        study_fingerprint=inputs.trial.user_attrs["study_fingerprint"],
        data_fingerprint=_validation_data_fingerprint(
            inputs.replay, inputs.settings.acceptance_gate
        ),
        trajectory_fingerprint=inputs.artifacts.trajectory_fingerprint,
        prefix_set_sha256=_prefix_fingerprint(inputs.settings),
    )


def make_heretic_export_context(inputs: HereticExportInputs) -> ExportContext:
    """Adapt loaded Heretic runtime objects to the staged export callbacks."""
    from .ara_runtime import capture_adapter_factor_state, runtime_report_dict
    from .artifact_schema import reproduction_metadata
    from .trial_methods import (
        TrajectoryArtifacts,
        apply_trial,
        cleanup_trial,
        parameters_from_trial,
    )
    from .workflow import _release_model_for_reload

    if not isinstance(inputs.artifacts, TrajectoryArtifacts):
        raise TypeError("trajectory-v2 export requires TrajectoryArtifacts")
    parameters = parameters_from_trial(inputs.trial)
    ledger_path = inputs.evidence_directory / "audit-ledger.json"

    def apply(candidate: FrozenTrial) -> None:
        apply_trial(inputs.model, parameters_from_trial(candidate), inputs.artifacts)

    def capture() -> Mapping[str, Tensor]:
        return capture_adapter_factor_state(inputs.model.ara_targets)

    def cleanup() -> None:
        cleanup_trial(inputs.model, inputs.artifacts)

    report = runtime_report_dict(inputs.model.ara_runtime_report)
    return ExportContext(
        destination=inputs.destination,
        evidence_directory=inputs.evidence_directory,
        gate=inputs.settings.acceptance_gate,
        identities=_export_identities(inputs),
        third_apply=apply,
        capture_state=capture,
        cleanup=cleanup,
        write_core=_core_writer(inputs, parameters),
        smoke=lambda _staging: _release_model_for_reload(inputs.model),
        audit=_audit_callback(inputs, ledger_path),
        study_summary=inputs.study_summary,
        runtime_guard_report=report,
        reproduction_metadata=reproduction_metadata(inputs.settings, inputs.trial),
    )


def _execute_unique_audit(
    context: ExportContext,
    staging: Path,
    ledger_path: Path,
) -> tuple[Sequence[Mapping[str, Any]], str]:
    if _read_ledger(ledger_path)["status"] == "not_started":
        transition_audit_ledger(ledger_path, "started")
    context.smoke(staging)
    records = context.audit(staging)
    if _read_ledger(ledger_path)["status"] != "consumed":
        raise AcceptanceGateError(
            "fresh audit worker did not persist audit consumption"
        )
    values = validate_gate_records(records, context.gate)
    if (
        values.keyword > context.gate.keyword_max
        or values.relative_keyword_drop < context.gate.keyword_drop_min
        or values.kl > context.gate.kl_max
    ):
        raise AcceptanceGateError("the unique audit did not pass its thresholds")
    transition_audit_ledger(ledger_path, "passed")
    shutil.copyfile(ledger_path, staging / "audit-ledger.json")
    return records, file_sha256(ledger_path)


def _write_bound_reports(
    context: ExportContext,
    trial: FrozenTrial,
    replay: ReplayEvidence,
    staging: Path,
    evidence: PublicationEvidence,
) -> tuple[bytes, bytes]:
    acceptance = _passed_acceptance(
        context,
        trial,
        replay,
        evidence.audit_records,
        (evidence.core_hashes, evidence.ledger_hash),
    )
    acceptance_bytes = atomic_write_json(staging / "acceptance.json", acceptance)
    reproduce = _reproduce_payload(
        context,
        trial,
        evidence.core_hashes,
        file_sha256(staging / "acceptance.json"),
    )
    reproduce_bytes = atomic_write_json(staging / "reproduce.json", reproduce)
    return acceptance_bytes, reproduce_bytes


def export_accepted_adapter(
    context: ExportContext,
    trial: FrozenTrial,
    replay: ReplayEvidence,
) -> ExportResult:
    """Perform third apply, one audit, hash verification, and atomic promotion."""
    ledger_path = context.evidence_directory / "audit-ledger.json"
    ensure_audit_available(ledger_path)
    ledger_status = _read_ledger(ledger_path)["status"]
    stale = context.destination.parent.glob(f".{context.destination.name}.staging-*")
    retry = ledger_status == "started" or (
        ledger_status == "not_started" and any(stale)
    )
    staging = _new_staging(context.destination, resume=retry)
    try:
        context.third_apply(trial)
        actual_state = context.capture_state()
        _compare_export_state(replay.second.adapter_state, actual_state)
        relative_core_paths = list(context.write_core(staging))
        core_hashes = core_artifact_hashes(staging, relative_core_paths)
        audit_records, ledger_hash = _execute_unique_audit(
            context, staging, ledger_path
        )
        acceptance_bytes, reproduce_bytes = _write_bound_reports(
            context,
            trial,
            replay,
            staging,
            PublicationEvidence(core_hashes, audit_records, ledger_hash),
        )
        verify_artifact_graph(staging)
        os.replace(staging, context.destination)
        return ExportResult(
            context.destination,
            acceptance_bytes,
            reproduce_bytes,
        )
    except BaseException as error:
        ledger = _read_ledger(ledger_path)
        if ledger["status"] == "consumed":
            transition_audit_ledger(
                ledger_path,
                "failed",
                {"error": " ".join(str(error).split())[:500]},
            )
        raise
    finally:
        context.cleanup()


def _copy_external_report(settings: Any, result: ExportResult) -> None:
    report_path = Path(settings.acceptance_gate.report_path)
    canonical = result.destination / "acceptance.json"
    if report_path.resolve() == canonical.resolve():
        return
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_name(f".{report_path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(result.acceptance_bytes)
    os.replace(temporary, report_path)


def _save_v2(context: SaveActionContext, destination: Path) -> None:
    from dataclasses import asdict

    from .ara_search import summarize_study

    if context.replay is None:
        raise RuntimeError("validation replay has not completed")
    summary = summarize_study(
        context.study,
        context.settings.acceptance_gate,
    )
    inputs = HereticExportInputs(
        destination=destination,
        evidence_directory=context.checkpoint_path.parent / "audit-evidence",
        settings=context.settings,
        model=context.model,
        artifacts=context.artifacts,
        trial=context.trial,
        replay=context.replay,
        study_summary=asdict(summary),
        checkpoint_path=context.checkpoint_path,
    )
    result = export_accepted_adapter(
        make_heretic_export_context(inputs), context.trial, context.replay
    )
    _copy_external_report(context.settings, result)


def _save_v1(context: SaveActionContext, destination: Path) -> None:
    from . import workflow

    if context.audit_records is None:
        raise RuntimeError("acceptance audit has not completed")
    evidence = workflow.AcceptedExport(
        runtime=workflow.AcceptanceRuntime(
            context.settings,
            context.model,
            context.artifacts,
            context.evaluator,
        ),
        trial=context.trial,
        audit_scores=[dict(record) for record in context.audit_records],
        study_fingerprint=context.study_fingerprint,
        calibration_fingerprint=context.calibration_fingerprint,
        checkpoint_path=str(context.checkpoint_path),
    )
    workflow.export_accepted_adapter(destination, evidence)


def _audit_was_consumed(checkpoint_path: Path) -> bool:
    ledger = checkpoint_path.parent / "audit-evidence" / "audit-ledger.json"
    if not ledger.is_file():
        return False
    try:
        return bool(json.loads(ledger.read_text(encoding="utf-8"))["audit_consumed"])
    except (KeyError, OSError, ValueError):
        return True


def _record_export_failure(
    context: SaveActionContext,
    error: BaseException,
) -> None:
    from .acceptance import failed_acceptance_payload
    from .trial_methods import (
        AcceptanceReport,
        parameter_envelope,
        parameters_from_trial,
        safe_failure_reason,
        write_acceptance_report,
    )

    settings = context.settings
    if settings.ara_objective_version == "trajectory-v2":
        payload = failed_acceptance_payload(
            context.study,
            settings.acceptance_gate,
            context.study_fingerprint,
            "export-or-audit",
            _audit_was_consumed(context.checkpoint_path),
        )
        payload["selected_trial_number"] = context.trial.number
        payload["failure_reason"] = safe_failure_reason(error)
        atomic_write_json(settings.acceptance_gate.report_path, payload)
        return
    report = AcceptanceReport(
        status="failed",
        reason=safe_failure_reason(error),
        model_fingerprint=context.model.model_fingerprint,
        study_fingerprint=context.study_fingerprint,
        calibration_fingerprint=context.calibration_fingerprint,
        selected_trial_number=context.trial.number,
        parameters=parameter_envelope(parameters_from_trial(context.trial)),
        audit_scores=[dict(record) for record in context.audit_records or ()],
    )
    write_acceptance_report(settings.acceptance_gate.report_path, report)


def _save_gated(context: SaveActionContext, destination: Path) -> None:
    try:
        if context.settings.ara_objective_version == "trajectory-v2":
            _save_v2(context, destination)
        else:
            _save_v1(context, destination)
    except BaseException as error:
        _record_export_failure(context, error)
        raise


def _save_ungated(
    context: SaveActionContext,
    destination: Path,
    strategy: ExportStrategy,
) -> None:
    if strategy == ExportStrategy.ADAPTER:
        rich_print("Saving LoRA adapter...")
        context.model.model.save_pretrained(
            destination,
            max_shard_size=context.settings.max_shard_size,
        )
        return
    rich_print("Saving merged model...")
    merged = context.model.get_merged_model()
    merged.save_pretrained(
        destination,
        max_shard_size=context.settings.max_shard_size,
    )
    del merged
    empty_cache()
    context.model.tokenizer.save_pretrained(destination)
    if context.model.processor is not None:
        context.model.processor.save_pretrained(destination)
    context.reset_model()


def _verify_local_reproduction(
    destination: Path,
    reproduction: Mapping[str, Any] | None,
) -> None:
    from .artifact_schema import verify_reproduced_weights

    verify_reproduced_weights(destination, reproduction)


def run_save_action(context: SaveActionContext) -> bool:
    """Run local export and return whether the enclosing menu should close."""
    if getattr(context.settings, "ara_objective_version", None) == "sequential-v3":
        raise ValueError("v3 仅允许通过冻结集合的离线 finalize 提升制品")
    from .workflow import obtain_export_strategy

    selected = context.settings.save_directory
    if selected is None:
        selected = ask_if_unset(
            selected,
            questionary.path("Path to the folder:", only_directories=True),
        )
    if not selected:
        return False
    destination = Path(selected)
    strategy = obtain_export_strategy(context.settings, context.model)
    if strategy is None:
        return False
    is_v2_reproduction = (
        context.reproduction is not None
        and context.reproduction.get("schema") == REPRODUCE_SCHEMA
    )
    if is_v2_reproduction:
        if strategy != ExportStrategy.ADAPTER:
            raise ValueError("trajectory-v2 reproduction requires adapter strategy")
        # Validation replay is transactional and restores the identity adapter.
        # Reapply the reproduced candidate immediately before writing its factors.
        context.reset_model()
        _save_ungated(context, destination, strategy)
    elif context.settings.acceptance_gate is not None:
        if strategy != ExportStrategy.ADAPTER:
            raise ValueError("CARA acceptance export requires adapter strategy")
        _save_gated(context, destination)
    else:
        _save_ungated(context, destination, strategy)
    _verify_local_reproduction(destination, context.reproduction)
    rich_print(f"Model saved to [bold]{destination}[/].")
    return context.settings.acceptance_gate is not None


def _select_action(settings: Any) -> str | None:
    if settings.model_action is not None:
        return settings.model_action
    return ask_if_unset(
        None,
        questionary.select(
            "What do you want to do with the decensored model?",
            choices=[
                Choice("Save the model to a local folder", value="save"),
                Choice("Upload the model to Hugging Face", value="upload"),
                Choice("Chat with the model", value="chat"),
                Choice("Run benchmarks", value="benchmark"),
                Choice("Select a different trial", value=""),
            ],
            style=Style([("highlighted", "reverse")]),
        ),
    )


def run_model_actions(context: SaveActionContext) -> None:
    """Dispatch interactive model actions, propagating every gated failure."""
    from .ara_runtime import (
        UploadActionContext,
        run_benchmark_session,
        run_chat_session,
        run_upload_action,
    )

    while True:
        action = _select_action(context.settings)
        if action in (None, ""):
            return
        try:
            if action == "save" and run_save_action(context):
                return
            if action == "upload":
                run_upload_action(
                    UploadActionContext(
                        context.settings,
                        context.model,
                        context.evaluator,
                        context.study,
                        context.trial,
                        str(context.checkpoint_path),
                        context.reproduction,
                        context.reset_model,
                    )
                )
            elif action == "chat":
                run_chat_session(context.model, context.settings.system_prompt)
            elif action == "benchmark":
                run_benchmark_session(context.settings, context.model)
        except Exception as error:
            if context.settings.acceptance_gate is not None:
                raise
            message = format_exception(error)
            separator = ":\n" if "\n" in message else ": "
            rich_print(f"[red]Error{separator}{message}[/]")
        if context.settings.model_action is not None:
            return
