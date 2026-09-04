# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import torch
from optuna.trial import create_trial

from heretic.acceptance import (
    AcceptanceGateError,
    ReplayEvidence,
    ReplayResult,
)
from heretic.acceptance_export import (
    ExportContext,
    ExportIdentities,
    SaveActionContext,
    _select_action,
    ensure_audit_available,
    export_accepted_adapter,
    run_save_action,
    transition_audit_ledger,
)
from heretic.artifact_schema import (
    ACCEPTANCE_SCHEMA,
    REPRODUCE_SCHEMA,
    atomic_write_json,
    canonical_sha256,
    core_artifact_hashes,
    file_sha256,
    load_bound_acceptance,
    validate_reproduce_v2,
    verify_artifact_graph,
)
from heretic.config import AcceptanceGate, DatasetSpecification, ExportStrategy

TRAJECTORY_MANIFEST = {"protocol": "test-trajectory"}
TRAJECTORY_HASH = canonical_sha256(TRAJECTORY_MANIFEST)
PARAMETERS = {
    "method": "ara",
    "objective_version": "trajectory-v2",
    "search_space_version": "trajectory-v2-eight-dimensional-v1",
    "payload": {
        "layer_start_fraction": 0.2,
        "layer_span_fraction": 0.4,
        "start_layer_index": 1,
        "end_layer_index": 3,
        "attn_strength": 0.5,
        "mlp_strength": 0.25,
        "push_weight": 1.0,
        "margin": 4.0,
        "attn_deployment_gain": 1.0,
        "mlp_deployment_gain": 1.0,
    },
}


def gate() -> AcceptanceGate:
    prompts = DatasetSpecification(
        dataset="org/data", commit="abc", split="test[:100]", column="text"
    )
    return AcceptanceGate(
        keyword_score="Keywords",
        keyword_max=0.1,
        keyword_drop_min=0.5,
        kl_score="KL divergence",
        kl_max=0.15,
        expected_samples=100,
        keyword_audit_prompts=prompts,
        kl_audit_prompts=prompts,
    )


def score_records() -> list[dict]:
    def evidence(value: float, fingerprint: str) -> dict:
        return {
            "value": value,
            "sample_count": 100,
            "dataset_fingerprint": fingerprint,
        }

    return [
        {
            "name": "Keywords",
            "score": evidence(0.05, "harmful"),
            "baseline": evidence(0.7, "harmful"),
        },
        {
            "name": "KL divergence",
            "score": evidence(0.1, "good"),
            "baseline": evidence(0.0, "good"),
        },
    ]


def replay(state: dict[str, torch.Tensor]) -> ReplayEvidence:
    result = ReplayResult(tuple(score_records()), state)
    return ReplayEvidence(result, result, 0.0, 0.0, 0.0)


def trial():
    return create_trial(
        values=[0.0],
        user_attrs={"ara_parameters": PARAMETERS},
    )


def write_core(staging: Path) -> list[str]:
    (staging / "adapter_config.json").write_text("{}", encoding="utf-8")
    (staging / "adapter_model.safetensors").write_bytes(b"adapter")
    (staging / "effective-config.toml").write_text("model='test'", encoding="utf-8")
    (staging / "README.md").write_text("# Adapter", encoding="utf-8")
    (staging / "study.jsonl").write_text("{}\n", encoding="utf-8")
    atomic_write_json(staging / "trajectory-manifest.json", TRAJECTORY_MANIFEST)
    atomic_write_json(
        staging / "study-identity.json",
        {
            "study_fingerprint": "study",
            "trial_number": -1,
            "parameters": PARAMETERS,
        },
    )
    return [
        "adapter_config.json",
        "adapter_model.safetensors",
        "effective-config.toml",
        "README.md",
        "study.jsonl",
        "trajectory-manifest.json",
        "study-identity.json",
    ]


def export_context(
    root: Path,
    capture,
    audit,
    cleanup,
) -> ExportContext:
    return ExportContext(
        destination=root / "adapter",
        evidence_directory=root / "evidence",
        gate=gate(),
        identities=ExportIdentities(
            "model", "study", "data", TRAJECTORY_HASH, "prefix"
        ),
        third_apply=lambda _trial: None,
        capture_state=capture,
        cleanup=cleanup,
        write_core=write_core,
        smoke=lambda _staging: None,
        audit=audit,
        study_summary={},
        runtime_guard_report={},
    )


class AcceptanceExportTests(unittest.TestCase):
    def test_configured_model_action_does_not_construct_prompt(self) -> None:
        with patch(
            "heretic.acceptance_export.questionary.select",
            side_effect=AssertionError("prompt constructed"),
        ):
            self.assertEqual(
                _select_action(SimpleNamespace(model_action="save")), "save"
            )

    def test_v2_reproduction_reapplies_candidate_before_saving(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reset = Mock()
            context = SaveActionContext(
                settings=SimpleNamespace(
                    save_directory=directory,
                    export_strategy=ExportStrategy.ADAPTER,
                    acceptance_gate=gate(),
                ),
                model=Mock(),
                evaluator=Mock(),
                artifacts=Mock(),
                study=None,
                trial=trial(),
                replay=replay({"factor": torch.tensor([1.0])}),
                audit_records=None,
                study_fingerprint="study",
                calibration_fingerprint=None,
                checkpoint_path=Path(directory) / "study.jsonl",
                reproduction={"schema": REPRODUCE_SCHEMA, "hashes": {}},
                reset_model=reset,
            )
            with (
                patch("heretic.acceptance_export._save_ungated") as save,
                patch("heretic.acceptance_export._verify_local_reproduction"),
                patch(
                    "heretic.workflow.obtain_export_strategy",
                    return_value=ExportStrategy.ADAPTER,
                ),
            ):
                self.assertTrue(run_save_action(context))
            reset.assert_called_once_with()
            save.assert_called_once()

    def test_preconsume_started_ledger_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "audit-ledger.json"
            transition_audit_ledger(ledger, "started")
            ensure_audit_available(ledger)

    def test_preledger_export_failure_can_retry_same_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "evidence" / "audit-ledger.json"
            attempts = 0

            def retrying_core(staging):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    (staging / "partial.txt").write_text("partial", encoding="utf-8")
                    raise OSError("temporary storage failure")
                return write_core(staging) + ["partial.txt"]

            def passing_audit(_staging):
                transition_audit_ledger(ledger, "consumed")
                return score_records()

            context = export_context(
                root,
                lambda: {"factor": torch.tensor([1.0])},
                passing_audit,
                lambda: None,
            )
            context = ExportContext(**{**context.__dict__, "write_core": retrying_core})
            with self.assertRaisesRegex(OSError, "temporary storage"):
                export_accepted_adapter(
                    context, trial(), replay({"factor": torch.tensor([1.0])})
                )
            result = export_accepted_adapter(
                context, trial(), replay({"factor": torch.tensor([1.0])})
            )
            self.assertEqual(result.destination, root / "adapter")
            self.assertEqual(attempts, 2)

    def test_consumed_audit_ledger_cannot_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "audit-ledger.json"
            transition_audit_ledger(ledger, "started")
            transition_audit_ledger(ledger, "consumed")
            with self.assertRaises(AcceptanceGateError):
                ensure_audit_available(ledger)

    def test_core_acceptance_reproduce_graph_is_acyclic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hashes = core_artifact_hashes(root, write_core(root))
            atomic_write_json(
                root / "audit-ledger.json",
                {
                    "schema": "cara-audit-ledger-v1",
                    "status": "passed",
                    "audit_consumed": True,
                },
            )
            acceptance = {
                "schema": ACCEPTANCE_SCHEMA,
                "status": "passed",
                "model_fingerprint": "model",
                "study_fingerprint": "study",
                "data_fingerprint": "data",
                "trajectory_fingerprint": TRAJECTORY_HASH,
                "selected_trial_number": -1,
                "validation_replays": {
                    "first": score_records(),
                    "second": score_records(),
                    "max_absolute_error": 0.0,
                    "max_relative_error": 0.0,
                    "kl_drift": 0.0,
                },
                "audit": score_records(),
                "audit_consumed": True,
                "audit_ledger_sha256": file_sha256(root / "audit-ledger.json"),
                "runtime_guard_report": {},
                "study_summary": {},
                "core_artifact_hashes": hashes,
            }
            atomic_write_json(root / "acceptance.json", acceptance)
            reproduce = {
                "schema": REPRODUCE_SCHEMA,
                "objective_version": "trajectory-v2",
                "trajectory_manifest_sha256": TRAJECTORY_HASH,
                "prefix_set_sha256": "prefix",
                "search_space_version": "space",
                "sampler_protocol": "sampler",
                "core_artifact_hashes": hashes,
                "acceptance_sha256": file_sha256(root / "acceptance.json"),
                "model_fingerprint": "model",
                "study_fingerprint": "study",
                "data_fingerprint": "data",
                "selected_trial_number": -1,
                "parameters": PARAMETERS,
            }
            atomic_write_json(root / "reproduce.json", reproduce)
            verify_artifact_graph(root)
            self.assertEqual(
                load_bound_acceptance(str(root / "reproduce.json"), reproduce, True),
                acceptance,
            )
            report = json.loads((root / "acceptance.json").read_text())
            self.assertNotIn("acceptance.json", report["core_artifact_hashes"])

    def test_graph_rejects_unbound_adapter_weights(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = write_core(root)
            paths.remove("adapter_model.safetensors")
            hashes = core_artifact_hashes(root, paths)
            with self.assertRaisesRegex(ValueError, "mandatory adapter"):
                validate_reproduce_v2(
                    {
                        "schema": REPRODUCE_SCHEMA,
                        "objective_version": "trajectory-v2",
                        "trajectory_manifest_sha256": TRAJECTORY_HASH,
                        "prefix_set_sha256": "prefix",
                        "search_space_version": "space",
                        "sampler_protocol": "sampler",
                        "core_artifact_hashes": hashes,
                        "acceptance_sha256": "acceptance",
                        "model_fingerprint": "model",
                        "study_fingerprint": "study",
                        "data_fingerprint": "data",
                        "selected_trial_number": 0,
                        "parameters": PARAMETERS,
                    }
                )

    def test_third_apply_drift_retains_staging_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cleanup = Mock()
            context = export_context(
                root,
                lambda: {"factor": torch.tensor([2.0])},
                lambda _staging: score_records(),
                cleanup,
            )
            with self.assertRaises(AcceptanceGateError):
                export_accepted_adapter(
                    context,
                    trial(),
                    replay({"factor": torch.tensor([1.0])}),
                )
            self.assertFalse(context.destination.exists())
            self.assertEqual(len(list(root.glob(".adapter.staging-*"))), 1)
            cleanup.assert_called_once()

    def test_consumed_audit_failure_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "evidence" / "audit-ledger.json"

            def failed_audit(_staging):
                transition_audit_ledger(ledger, "consumed")
                raise RuntimeError("worker crashed after consumption")

            context = export_context(
                root,
                lambda: {"factor": torch.tensor([1.0])},
                failed_audit,
                lambda: None,
            )
            with self.assertRaisesRegex(RuntimeError, "worker crashed"):
                export_accepted_adapter(
                    context,
                    trial(),
                    replay({"factor": torch.tensor([1.0])}),
                )
            self.assertEqual(json.loads(ledger.read_text())["status"], "failed")
            with self.assertRaises(AcceptanceGateError):
                ensure_audit_available(ledger)

    def test_audit_threshold_failure_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "evidence" / "audit-ledger.json"

            def failing_audit(_staging):
                transition_audit_ledger(ledger, "consumed")
                records = score_records()
                records[0]["score"]["value"] = 0.2
                return records

            context = export_context(
                root,
                lambda: {"factor": torch.tensor([1.0])},
                failing_audit,
                lambda: None,
            )
            with self.assertRaisesRegex(AcceptanceGateError, "thresholds"):
                export_accepted_adapter(
                    context,
                    trial(),
                    replay({"factor": torch.tensor([1.0])}),
                )
            self.assertEqual(json.loads(ledger.read_text())["status"], "failed")
            self.assertFalse(context.destination.exists())

    def test_success_atomically_promotes_and_verifies_graph(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "evidence" / "audit-ledger.json"

            def passing_audit(_staging):
                transition_audit_ledger(ledger, "consumed")
                return score_records()

            context = export_context(
                root,
                lambda: {"factor": torch.tensor([1.0])},
                passing_audit,
                lambda: None,
            )
            result = export_accepted_adapter(
                context,
                trial(),
                replay({"factor": torch.tensor([1.0])}),
            )
            self.assertEqual(result.destination, context.destination)
            self.assertTrue(context.destination.is_dir())
            self.assertFalse(list(root.glob(".adapter.staging-*")))
            self.assertEqual(json.loads(ledger.read_text())["status"], "passed")
            verify_artifact_graph(context.destination)


if __name__ == "__main__":
    unittest.main()
