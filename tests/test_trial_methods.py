# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026  Philipp Emanuel Weidmann <pew@worldwidemann.com> + contributors

import math
import sys
import unittest
from typing import cast
from unittest.mock import patch

import optuna
import torch
from optuna import Trial
from optuna.trial import FixedTrial, TrialState, create_trial

from heretic.ara import ARAComponentParameters, ARAParameters
from heretic.config import (
    AbliterationMethod,
    AcceptanceGate,
    DatasetSpecification,
    RowNormalization,
    Settings,
)
from heretic.trial_methods import (
    AcceptanceGateError,
    DirectionalArtifacts,
    MethodContext,
    build_study_fingerprint,
    cleanup_trial,
    parameter_envelope,
    parse_parameter_envelope,
    sample_method_parameters,
    select_accepted_trial,
)
from heretic.workflow import select_for_acceptance


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


def records(keyword: float, divergence: float, samples: int = 100):
    keyword_count = round(keyword * samples) if math.isfinite(keyword) else 0
    return [
        {
            "name": "Keywords",
            "score": {
                "value": keyword,
                "md_display": f"{keyword_count}/{samples}",
            },
            "baseline": {"value": 0.7, "md_display": f"70/{samples}"},
        },
        {
            "name": "KL divergence",
            "score": {"value": divergence, "md_display": str(divergence)},
            "baseline": {"value": 0.0, "md_display": "0"},
        },
    ]


def trial(
    method="ara",
    fingerprint="study",
    keyword=0.1,
    divergence=0.1,
    state=TrialState.COMPLETE,
):
    return create_trial(
        state=state,
        values=[keyword if math.isfinite(keyword) else 0.0, divergence]
        if state == TrialState.COMPLETE
        else None,
        user_attrs={
            "method": method,
            "study_fingerprint": fingerprint,
            "scores": records(keyword, divergence),
        },
    )


class TrialMethodTests(unittest.TestCase):
    def test_ara_parameter_envelope_round_trip(self) -> None:
        original = ARAParameters(
            2,
            5,
            {"attn.o_proj": ARAComponentParameters(0.2, 0.8, 1.4)},
        )
        self.assertEqual(
            parse_parameter_envelope(parameter_envelope(original)), original
        )

    def test_gate_filters_and_uses_lexicographic_order(self) -> None:
        study = optuna.create_study(directions=["minimize", "minimize"])
        for item in (
            trial(keyword=0.08, divergence=0.12),
            trial(keyword=0.04, divergence=0.14),
            trial(keyword=0.04, divergence=0.09),
            trial(method="directional", keyword=0.0, divergence=0.0),
            trial(fingerprint="wrong", keyword=0.0, divergence=0.0),
            trial(keyword=0.11, divergence=0.01),
        ):
            study.add_trial(item)
        selected = select_accepted_trial(study.trials, gate(), "study")
        self.assertEqual(selected.number, 2)

    def test_gate_rejects_nonfinite_and_wrong_sample_count(self) -> None:
        for item in (trial(keyword=math.nan), trial(keyword=0.05)):
            if math.isfinite(item.user_attrs["scores"][0]["score"]["value"]):
                item.user_attrs["scores"] = records(0.05, 0.1, 99)
            with self.subTest(item=item):
                with self.assertRaises(AcceptanceGateError):
                    select_accepted_trial([item], gate(), "study")

    def test_gate_rejects_scores_outside_unit_interval(self) -> None:
        for item in (trial(keyword=-0.1), trial(divergence=-0.1)):
            with self.subTest(item=item), self.assertRaises(AcceptanceGateError):
                select_accepted_trial([item], gate(), "study")

    def test_gate_rejects_missing_and_duplicate_score_names(self) -> None:
        missing = trial()
        missing.user_attrs["scores"] = missing.user_attrs["scores"][:1]
        duplicate = trial()
        duplicate.user_attrs["scores"].append(duplicate.user_attrs["scores"][0])
        for item in (missing, duplicate):
            with self.subTest(item=item), self.assertRaises(AcceptanceGateError):
                select_accepted_trial([item], gate(), "study")

    def test_qwen_gate_requires_full_preregistered_trial_count(self) -> None:
        trials = [trial() for _ in range(110)]
        with self.assertRaisesRegex(AcceptanceGateError, "120 required"):
            select_for_acceptance(trials, gate(), "study", "Qwen/Qwen3.8-27B")
        trials.extend(trial(state=TrialState.PRUNED) for _ in range(10))
        self.assertIs(
            select_for_acceptance(trials, gate(), "study", "Qwen/Qwen3.8-27B"),
            trials[0],
        )

    def test_ara_search_space_has_fixed_dimensions(self) -> None:
        raw = {
            "ara.layer_start_fraction": 0.4,
            "ara.layer_span_fraction": 0.3,
            "ara.attn_strength": 0.1,
            "ara.mlp_strength_raw": -0.05,
            "ara.push_weight": 0.8,
            "ara.margin": 1.2,
        }
        fixed = FixedTrial(raw)
        with patch.object(sys, "argv", ["test"]):
            settings = Settings(
                model="org/model",
                abliteration_method=AbliterationMethod.ARA,
                row_normalization=RowNormalization.NONE,
            )
        result = cast(
            ARAParameters,
            sample_method_parameters(
                cast(Trial, fixed),
                MethodContext(settings, 64, ("attn.o_proj", "mlp.down_proj")),
            ),
        )
        self.assertEqual(set(fixed.params), set(raw))
        self.assertEqual((result.start_layer_index, result.end_layer_index), (25, 45))
        self.assertEqual(result.components["mlp.down_proj"].strength, 0.0)

    def test_directional_cleanup_resets_model(self) -> None:
        class FakeModel:
            resets = 0

            def reset_model(self) -> None:
                self.resets += 1

        model = FakeModel()
        cleanup_trial(model, DirectionalArtifacts(torch.zeros(1)))  # type: ignore[arg-type]
        self.assertEqual(model.resets, 1)

    def test_fingerprint_tracks_semantics_not_output_path(self) -> None:
        with patch.object(sys, "argv", ["test"]):
            base = Settings(model="org/model", seed=4)
            output_change = base.model_copy(update={"save_directory": "elsewhere"})
            semantic_change = base.model_copy(update={"system_prompt": "different"})
        self.assertEqual(
            build_study_fingerprint(base), build_study_fingerprint(output_change)
        )
        self.assertNotEqual(
            build_study_fingerprint(base), build_study_fingerprint(semantic_change)
        )
        first_gate = gate()
        second_gate = first_gate.model_copy(update={"keyword_max": 0.05})
        first = base.model_copy(update={"acceptance_gate": first_gate})
        second = base.model_copy(update={"acceptance_gate": second_gate})
        self.assertNotEqual(
            build_study_fingerprint(first), build_study_fingerprint(second)
        )


if __name__ == "__main__":
    unittest.main()
