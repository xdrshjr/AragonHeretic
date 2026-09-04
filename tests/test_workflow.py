# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026  Philipp Emanuel Weidmann <pew@worldwidemann.com> + contributors

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from optuna.trial import create_trial

from heretic.config import (
    AcceptanceGate,
    DatasetSpecification,
    ExportStrategy,
    QuantizationMethod,
    Settings,
)
from heretic.trial_methods import AcceptanceReport, parameters_from_trial
from heretic.workflow import (
    AcceptanceRuntime,
    AcceptedExport,
    export_accepted_adapter,
    make_reproduction_trial,
    obtain_export_strategy,
    replay_candidate,
    run_acceptance_gate,
    select_for_acceptance,
    verify_reloaded_adapter,
)


def gate(report_path: str = "acceptance.json") -> AcceptanceGate:
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
        report_path=report_path,
    )


def records(keyword: float = 0.05, divergence: float = 0.1) -> list[dict[str, Any]]:
    return [
        {
            "name": "Keywords",
            "score": {"value": keyword, "md_display": "5/100"},
            "baseline": {"value": 0.7, "md_display": "70/100"},
        },
        {
            "name": "KL divergence",
            "score": {"value": divergence, "md_display": "0.1"},
            "baseline": {"value": 0.0, "md_display": "0"},
        },
    ]


def ara_trial():
    return create_trial(
        values=[],
        user_attrs={
            "method": "ara",
            "ara_parameters": {
                "start_layer_index": 0,
                "end_layer_index": 1,
                "components": {
                    "attn.o_proj": {
                        "strength": 0.1,
                        "push_weight": 0.5,
                        "margin": 1.0,
                    }
                },
            },
            "scores": records(),
        },
    )


class AcceptanceWorkflowTests(unittest.TestCase):
    def test_configured_export_strategy_does_not_construct_prompt(self) -> None:
        settings = SimpleNamespace(
            export_strategy=ExportStrategy.ADAPTER,
            quantization=QuantizationMethod.NONE,
        )
        with patch(
            "heretic.workflow.questionary.select",
            side_effect=AssertionError("prompt constructed"),
        ):
            self.assertEqual(
                obtain_export_strategy(cast(Settings, settings), Mock()),
                ExportStrategy.ADAPTER,
            )

    def test_v2_reproduction_trial_preserves_full_envelope(self) -> None:
        payload = {
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
        }
        envelope = {
            "method": "ara",
            "objective_version": "trajectory-v2",
            "search_space_version": "trajectory-v2-eight-dimensional-v1",
            "payload": payload,
        }
        trial = make_reproduction_trial(
            {"schema": "cara-reproduce-v2", "scores": []}, envelope
        )
        self.assertEqual(trial.user_attrs["ara_parameters"], envelope)
        self.assertEqual(parameters_from_trial(trial).start_layer_index, 1)

    def test_local_model_path_uses_generic_candidate_selection(self) -> None:
        candidate = ara_trial()
        configured_gate = gate().model_copy(
            update={"required_trials": 1, "min_complete_trials": 1}
        )
        with patch(
            "heretic.workflow.select_accepted_trial",
            return_value=candidate,
        ) as select:
            selected = select_for_acceptance(
                [candidate], configured_gate, "study", "D:/models/Qwen-local"
            )
        self.assertIs(selected, candidate)
        select.assert_called_once()

    def test_missing_candidate_is_not_silently_ignored(self) -> None:
        configured_gate = gate().model_copy(
            update={"required_trials": 1, "min_complete_trials": 1}
        )
        with (
            patch(
                "heretic.workflow.select_accepted_trial",
                side_effect=RuntimeError("no candidate"),
            ),
            self.assertRaisesRegex(RuntimeError, "no candidate"),
        ):
            select_for_acceptance(
                [ara_trial()], configured_gate, "study", "local/model"
            )

    def test_replay_always_cleans_up_after_scorer_failure(self) -> None:
        evaluator = Mock()
        evaluator.get_scores.side_effect = RuntimeError("scorer failed")
        runtime = cast(
            AcceptanceRuntime,
            SimpleNamespace(
                model=SimpleNamespace(ara_targets=()),
                evaluator=evaluator,
                artifacts=object(),
            ),
        )
        with (
            patch("heretic.workflow.apply_trial"),
            patch("heretic.workflow.cleanup_trial") as cleanup,
            patch("heretic.workflow._capture_adapter_state", return_value="state"),
            self.assertRaisesRegex(RuntimeError, "scorer failed"),
        ):
            replay_candidate(runtime, ara_trial())
        cleanup.assert_called_once_with(runtime.model, runtime.artifacts)

    def test_gate_orders_two_replays_before_single_audit(self) -> None:
        events = []
        runtime = cast(
            AcceptanceRuntime,
            SimpleNamespace(
                settings=SimpleNamespace(acceptance_gate=gate()),
                model=object(),
                artifacts=object(),
                evaluator=object(),
            ),
        )
        audit = Mock()
        audit.get_scores.return_value = "scores"
        audit.get_paired_score_records.return_value = records()

        def replay(*_args):
            events.append("replay")
            return records(), "state"

        def make_audit(*_args):
            events.append("audit")
            return audit

        with (
            patch("heretic.workflow.replay_candidate", side_effect=replay),
            patch("heretic.workflow._adapter_states_allclose", return_value=True),
            patch("heretic.workflow.audit_settings", return_value=object()),
            patch("heretic.workflow.Evaluator", side_effect=make_audit),
            patch("heretic.workflow.parameters_from_trial", return_value=object()),
            patch("heretic.workflow.apply_trial"),
            patch("heretic.workflow._validate_audit_outputs"),
        ):
            result = run_acceptance_gate(runtime, ara_trial())
        self.assertEqual(events, ["replay", "replay", "audit"])
        self.assertEqual(result, records())

    def test_reload_process_rejects_timeout_failure_and_identity_drift(self) -> None:
        settings = cast(Settings, SimpleNamespace(model_dump=lambda: {}))

        def context(process, report):
            queue = SimpleNamespace(get_nowait=lambda: report)
            return SimpleNamespace(
                Queue=lambda: queue, Process=lambda **_kwargs: process
            )

        timeout = Mock(exitcode=None)
        timeout.is_alive.return_value = True
        with patch(
            "heretic.workflow.multiprocessing.get_context",
            return_value=context(timeout, {}),
        ):
            with self.assertRaises(TimeoutError):
                verify_reloaded_adapter(settings, Path("adapter"), "model")
        timeout.terminate.assert_called_once()

        for process, report, message in (
            (Mock(exitcode=1), {"status": "failed", "reason": "worker"}, "worker"),
            (
                Mock(exitcode=0),
                {"status": "passed", "model": "wrong", "scores": []},
                "fingerprint",
            ),
        ):
            process.is_alive.return_value = False
            with (
                self.subTest(message=message),
                patch(
                    "heretic.workflow.multiprocessing.get_context",
                    return_value=context(process, report),
                ),
                self.assertRaisesRegex(RuntimeError, message),
            ):
                verify_reloaded_adapter(settings, Path("adapter"), "model")

    def test_export_promotes_only_after_reload_and_reproduce_succeed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "accepted"
            acceptance = gate(str(destination / "acceptance.json"))
            settings = Mock(acceptance_gate=acceptance)
            settings.model_copy.return_value = Mock(
                acceptance_gate=acceptance.model_copy(deep=True)
            )
            model = SimpleNamespace(model_fingerprint="model")
            runtime = cast(
                AcceptanceRuntime,
                SimpleNamespace(settings=settings, model=model, artifacts=object()),
            )
            evidence = AcceptedExport(
                runtime, ara_trial(), records(), "study", "cal", "journal"
            )
            report = AcceptanceReport(
                "passed",
                "ok",
                "model",
                "study",
                "cal",
                artifact_hashes={"adapter": "hash"},
            )
            with (
                patch("heretic.workflow._save_staging_files"),
                patch(
                    "heretic.workflow._runtime_diagnostics",
                    return_value=({}, {}, {}),
                ),
                patch("heretic.workflow._release_model_for_reload"),
                patch(
                    "heretic.workflow.verify_reloaded_adapter",
                    return_value=records(),
                ),
                patch("heretic.workflow._write_accepted_model_card"),
                patch(
                    "heretic.workflow._artifact_hashes",
                    return_value={"adapter": "hash"},
                ),
                patch("heretic.workflow._acceptance_report", return_value=report),
                patch("heretic.workflow.write_acceptance_report"),
                patch("heretic.workflow.create_reproduce_folder") as reproduce,
            ):
                export_accepted_adapter(destination, evidence)
            self.assertTrue(destination.is_dir())
            reproduce.assert_called_once()

    def test_export_failure_never_promotes_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "accepted"
            settings = SimpleNamespace(acceptance_gate=gate(), max_shard_size="1GB")
            runtime = cast(
                AcceptanceRuntime,
                SimpleNamespace(
                    settings=settings,
                    model=SimpleNamespace(model_fingerprint="model"),
                    artifacts=object(),
                ),
            )
            evidence = AcceptedExport(
                runtime, ara_trial(), records(), "study", "cal", "journal"
            )
            with (
                patch("heretic.workflow._save_staging_files"),
                patch(
                    "heretic.workflow._runtime_diagnostics", return_value=({}, {}, {})
                ),
                patch("heretic.workflow._release_model_for_reload"),
                patch(
                    "heretic.workflow.verify_reloaded_adapter",
                    side_effect=TimeoutError("timeout"),
                ),
                self.assertRaises(TimeoutError),
            ):
                export_accepted_adapter(destination, evidence)
            self.assertFalse(destination.exists())
            self.assertTrue((Path(directory) / ".accepted.staging").is_dir())


if __name__ == "__main__":
    unittest.main()
