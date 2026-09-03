# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026  Philipp Emanuel Weidmann <pew@worldwidemann.com> + contributors

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from optuna.trial import create_trial

from heretic.config import AcceptanceGate, DatasetSpecification, Settings
from heretic.trial_methods import AcceptanceReport
from heretic.workflow import (
    AcceptanceRuntime,
    AcceptedExport,
    export_accepted_adapter,
    replay_candidate,
    run_acceptance_gate,
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
