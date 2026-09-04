# SPDX-License-Identifier: AGPL-3.0-or-later

import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import optuna
from optuna.trial import FixedTrial, TrialState, create_trial

from heretic.ara_config import (
    ARASeedTrial,
    ARASearchSpace,
    AcceptanceGate,
    DatasetSpecification,
)
from heretic.ara_search import (
    ARASamplingContext,
    calculate_candidate_constraints,
    constraints_from_trial,
    derive_sampler_seed,
    enqueue_anchor_prefix,
    parameter_envelope,
    parse_parameter_envelope,
    recover_orphaned_trials,
    sample_v2_parameters,
    summarize_study,
)
from heretic.study_runner import _selection_state


def gate() -> AcceptanceGate:
    audit = DatasetSpecification(
        dataset="org/data",
        commit="a" * 40,
        split="test[:100]",
        column="text",
    )
    return AcceptanceGate(
        keyword_score="Keywords",
        keyword_max=0.1,
        keyword_drop_min=0.5,
        kl_score="KL divergence",
        kl_max=0.15,
        keyword_audit_prompts=audit,
        kl_audit_prompts=audit,
    )


def records() -> list[dict]:
    evidence = {"sample_count": 100, "dataset_fingerprint": "dataset"}
    return [
        {
            "name": "Keywords",
            "score": {"value": 0.1, **evidence},
            "baseline": {"value": 1.0, **evidence},
        },
        {
            "name": "KL divergence",
            "score": {"value": 0.15, **evidence},
            "baseline": {"value": 0.0, **evidence},
        },
    ]


def anchors() -> list[ARASeedTrial]:
    return [
        ARASeedTrial(
            layer_start=0.16 + index * 0.01,
            layer_span=0.4,
            attn_strength=0.5,
            mlp_strength=0.5,
            push_weight=1.0,
            margin=4.0,
            attn_deployment_gain=1.0,
            mlp_deployment_gain=1.0,
        )
        for index in range(8)
    ]


class ARASearchTests(unittest.TestCase):
    def test_gated_zero_complete_study_reports_failure_not_interrupt(self) -> None:
        context = SimpleNamespace(
            study=SimpleNamespace(trials=[]),
            settings=SimpleNamespace(acceptance_gate=object()),
        )
        with (
            patch(
                "heretic.study_runner._automatic_candidate",
                side_effect=RuntimeError("gate failure"),
            ) as automatic,
            self.assertRaisesRegex(RuntimeError, "gate failure"),
        ):
            _selection_state(context)
        automatic.assert_called_once_with(context)

    def test_fixed_eight_dimensions_have_no_clamp_mapping(self) -> None:
        raw = {
            "layer_start": 0.2,
            "layer_span": 0.5,
            "attn_strength": 0.7,
            "mlp_strength": 0.0001,
            "push_weight": 2.0,
            "margin": 4.0,
            "attn_deployment_gain": 1.5,
            "mlp_deployment_gain": 1.0,
        }
        parameters = sample_v2_parameters(
            FixedTrial(raw), ARASamplingContext(64), ARASearchSpace()
        )
        self.assertEqual(len(raw), 8)
        self.assertEqual(parameters.mlp_strength, 0.0001)
        self.assertEqual(
            (parameters.start_layer_index, parameters.end_layer_index), (12, 44)
        )

    def test_per_attempt_seed_is_stable_and_phase_separated(self) -> None:
        self.assertEqual(
            derive_sampler_seed(42, "startup", 8),
            derive_sampler_seed(42, "startup", 8),
        )
        self.assertNotEqual(
            derive_sampler_seed(42, "startup", 8),
            derive_sampler_seed(42, "tpe", 8),
        )

    def test_candidate_constraints_use_relative_drop(self) -> None:
        constraints = calculate_candidate_constraints(records(), gate())
        self.assertTrue(
            all(
                math.isclose(value, 0.0, abs_tol=1e-12) or value < 0
                for value in constraints
            )
        )
        trial = create_trial(
            state=TrialState.COMPLETE,
            values=[0.0, 0.15],
            user_attrs={"candidate_constraints": list(constraints)},
        )
        self.assertEqual(constraints_from_trial(trial), constraints)

    def test_constraints_reject_nonpositive_keyword_baseline(self) -> None:
        invalid = records()
        invalid[0]["baseline"]["value"] = 0.0
        with self.assertRaisesRegex(ValueError, "positive"):
            calculate_candidate_constraints(invalid, gate())

    def test_parameter_envelope_round_trips_exact_boundaries(self) -> None:
        search = ARASearchSpace()
        raw = {name: getattr(search, name)[1] for name in ARASearchSpace.model_fields}
        parameters = sample_v2_parameters(
            FixedTrial(raw), ARASamplingContext(64), search
        )
        self.assertEqual(
            parse_parameter_envelope(parameter_envelope(parameters)), parameters
        )
        self.assertEqual(parameters.end_layer_index, 64)

    def test_interrupted_anchor_enqueue_repairs_only_missing_suffix(self) -> None:
        study = optuna.create_study(direction="minimize")
        expected = anchors()
        enqueue_anchor_prefix(study, expected[:3])
        self.assertEqual(len(study.trials), 3)
        enqueue_anchor_prefix(study, expected)
        self.assertEqual(len(study.trials), 8)
        self.assertTrue(all(item.state == TrialState.WAITING for item in study.trials))
        self.assertEqual(study.trials[7].user_attrs["anchor_index"], 7)

    def test_orphan_recovery_requires_exclusive_owner_and_records_failure(self) -> None:
        study = optuna.create_study(direction="minimize")
        study.ask()
        with self.assertRaisesRegex(RuntimeError, "exclusive"):
            recover_orphaned_trials(study, False)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recovery.jsonl"
            recovered = recover_orphaned_trials(study, True, path)
            self.assertEqual(recovered[0].category, "interrupted")
            self.assertTrue(path.read_text(encoding="utf-8").strip())
        self.assertEqual(study.trials[0].state, TrialState.FAIL)
        summary = summarize_study(study, gate())
        self.assertEqual(summary.runtime_failure_categories, {"interrupted": 1})

    def test_anchor_orphan_before_suggestion_keeps_registered_identity(self) -> None:
        study = optuna.create_study(direction="minimize")
        expected = anchors()
        enqueue_anchor_prefix(study, expected)
        study.ask()
        recover_orphaned_trials(study, True)
        enqueue_anchor_prefix(study, expected)
        self.assertEqual(len(study.trials), 8)
        self.assertEqual(study.trials[0].state, TrialState.FAIL)

    def test_study_summary_counts_runtime_failures_and_best_trial(self) -> None:
        study = optuna.create_study(direction="minimize")
        complete = create_trial(
            value=0.2,
            user_attrs={
                "score_records": records(),
                "candidate_constraints": [0.0, -0.4, 0.0],
            },
        )
        failed = create_trial(
            state=TrialState.FAIL,
            user_attrs={
                "failure_record": {
                    "category": "oom",
                    "is_runtime_failure": True,
                }
            },
        )
        study.add_trials([complete, failed])
        summary = summarize_study(study.trials, gate())
        self.assertEqual(summary.state_counts["COMPLETE"], 1)
        self.assertEqual(summary.runtime_failure_categories, {"oom": 1})
        self.assertEqual(summary.best_keyword_trial, 0)
        self.assertEqual(summary.pareto_trial_ids, (0,))
        self.assertEqual(
            set(summary.constraint_statistics),
            {"keyword_max", "keyword_drop_min", "kl_max"},
        )


if __name__ == "__main__":
    unittest.main()
