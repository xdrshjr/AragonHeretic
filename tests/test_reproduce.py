# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026  Philipp Emanuel Weidmann <pew@worldwidemann.com> + contributors

import sys
import tempfile
import unittest
from hashlib import sha256
from json import dumps
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from optuna.trial import create_trial

from heretic.config import AbliterationMethod, RowNormalization, Settings
from heretic.model import Model
from heretic.trial_methods import (
    AcceptanceGateError,
    normalize_reproduction_parameters,
    validate_acceptance_binding,
)
from heretic.utils import get_readme_intro
from heretic.workflow import (
    _RELOAD_SMOKE_PROMPTS,
    load_reproduction_acceptance,
    validate_reproduction_model,
)

DIRECTIONAL = {
    "direction_index": None,
    "abliteration_parameters": {
        "attn.o_proj": {
            "max_weight": 1.0,
            "max_weight_position": 4.0,
            "min_weight": 0.0,
            "min_weight_distance": 3.0,
        }
    },
}


class ReproductionSchemaTests(unittest.TestCase):
    def test_v3_reproduce_dispatch_rejects_mixed_schema_fields(self):
        from heretic.ara_research_runner import paired_parameters
        from heretic.artifact_schema import parse_reproduce

        value = {
            "schema": "cara-research-reproduce-v3",
            "protocol_hash": "p",
            "candidate_lock_hash": "c",
            "parameters": paired_parameters(42, 0).envelope(),
            "acceptance_sha256": "a",
            "core_hashes": {"adapter.json": "hash"},
        }
        self.assertEqual(parse_reproduce(value), value)
        self.assertEqual(
            normalize_reproduction_parameters(value), value["parameters"]
        )
        value["trajectory_fingerprint"] = "v2"
        with self.assertRaises(ValueError):
            parse_reproduce(value)

    def test_v3_migrates_to_directional_envelope(self) -> None:
        envelope = normalize_reproduction_parameters(
            {"version": "3", "parameters": DIRECTIONAL}
        )
        self.assertEqual(envelope["method"], "directional")
        self.assertEqual(envelope["payload"], DIRECTIONAL)

    def test_v4_round_trips_both_methods(self) -> None:
        ara = {
            "method": "ara",
            "payload": {
                "start_layer_index": 1,
                "end_layer_index": 3,
                "components": {
                    "attn.o_proj": {
                        "strength": 0.1,
                        "push_weight": 0.5,
                        "margin": 1.0,
                    }
                },
            },
        }
        for envelope in (
            {"method": "directional", "payload": DIRECTIONAL},
            ara,
        ):
            self.assertEqual(
                normalize_reproduction_parameters(
                    {"version": "4", "parameters": envelope}
                ),
                envelope,
            )

    def test_unknown_version_and_method_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "version"):
            normalize_reproduction_parameters(
                {"version": "9", "parameters": {}}
            )
        with self.assertRaisesRegex(ValueError, "method"):
            normalize_reproduction_parameters(
                {
                    "version": "4",
                    "parameters": {"method": "other", "payload": {}},
                }
            )

    def test_acceptance_binding_rejects_hash_or_fingerprint_drift(self) -> None:
        hashes = {"adapter.safetensors": "abc"}
        parameters = {"method": "ara", "payload": {}}
        report = {
            "status": "passed",
            "parameters": parameters,
            "model_fingerprint": "model",
            "study_fingerprint": "study",
            "calibration_fingerprint": "calibration",
            "artifact_hashes": hashes,
        }
        reproduction = {
            "version": "4",
            "parameters": parameters,
            "model_fingerprint": "model",
            "study_fingerprint": "study",
            "calibration_fingerprint": "calibration",
            "hashes": hashes,
        }
        validate_acceptance_binding(report, reproduction, hashes)
        reproduction["study_fingerprint"] = "wrong"
        with self.assertRaises(AcceptanceGateError):
            validate_acceptance_binding(report, reproduction, hashes)

    def test_acceptance_report_loading_verifies_sha_and_model(self) -> None:
        hashes = {"adapter.safetensors": "abc"}
        parameters = {"method": "ara", "payload": {}}
        report = {
            "status": "passed",
            "parameters": parameters,
            "model_fingerprint": "model",
            "study_fingerprint": "study",
            "calibration_fingerprint": "calibration",
            "artifact_hashes": hashes,
        }
        payload = (dumps(report) + "\n").encode()
        binding = {
            "status": "passed",
            "path": "acceptance.json",
            "sha256": sha256(payload).hexdigest(),
        }
        reproduction = {
            "version": "4",
            "parameters": parameters,
            "model_fingerprint": "model",
            "study_fingerprint": "study",
            "calibration_fingerprint": "calibration",
            "hashes": hashes,
            "acceptance": binding,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reproduce_dir = root / "reproduce"
            reproduce_dir.mkdir()
            (root / "acceptance.json").write_bytes(payload)
            loaded = load_reproduction_acceptance(
                str(reproduce_dir / "reproduce.json"), reproduction, True
            )
            validate_reproduction_model(
                cast(Model, SimpleNamespace(model_fingerprint="model")), loaded
            )
            with self.assertRaisesRegex(AcceptanceGateError, "loaded model"):
                validate_reproduction_model(
                    cast(Model, SimpleNamespace(model_fingerprint="wrong")),
                    loaded,
                )
            binding["sha256"] = "wrong"
            with self.assertRaisesRegex(AcceptanceGateError, "report hash"):
                load_reproduction_acceptance(
                    str(reproduce_dir / "reproduce.json"), reproduction, True
                )

    def test_reload_smoke_prompts_match_committed_fixture(self) -> None:
        fixture = Path(__file__).parent / "qwen38-cara" / "smoke-prompts.txt"
        expected = tuple(
            line for line in fixture.read_text().splitlines() if line
        )
        self.assertEqual(_RELOAD_SMOKE_PROMPTS, expected)

    def test_model_card_names_cara_rank_and_gate_status(self) -> None:
        with patch.object(sys, "argv", ["test"]):
            settings = Settings(
                model="org/model",
                abliteration_method=AbliterationMethod.ARA,
                row_normalization=RowNormalization.NONE,
                ara_lora_rank=128,
            )
        trial = create_trial(
            values=[],
            user_attrs={
                "method": "ara",
                "ara_parameters": {
                    "start_layer_index": 1,
                    "end_layer_index": 2,
                    "components": {
                        "attn.o_proj": {
                            "strength": 0.1,
                            "push_weight": 0.5,
                            "margin": 1.0,
                        }
                    },
                },
                "scores": [],
                "acceptance_status": "passed",
            },
        )
        readme = get_readme_intro(settings, trial, False)
        self.assertIn("CARA-LoRA), rank 128", readme)
        self.assertIn("Acceptance gate:** passed", readme)


class NewResearchReproductionTests(unittest.TestCase):
    def test_loader_rejects_new_report_with_old_reproduction(self):
        from heretic.ara_research_acceptance import promote_research_artifact
        from heretic.ara_research_schema import read_json
        from heretic.artifact_schema import load_bound_acceptance
        from test_ara_research_acceptance import new_passed_report

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging, formal = root / "staging", root / "formal"
            promote_research_artifact(
                staging, formal, new_passed_report(staging)
            )
            reproduction = read_json(formal / "reproduce.json")
            reproduction["schema"] = "cara-research-reproduce-v3"
            for key in ("execution_identity_hash", "study_execution_hash"):
                reproduction.pop(key)
            with self.assertRaisesRegex(ValueError, "版本"):
                load_bound_acceptance(
                    str(formal / "reproduce.json"), reproduction, True
                )

    def test_new_reproduction_round_trip_and_parameter_binding(self):
        import tempfile
        from pathlib import Path
        from heretic.ara_research_acceptance import promote_research_artifact
        from heretic.reproduce import load_reproduction_information
        from heretic.artifact_schema import load_bound_acceptance
        from test_ara_research_acceptance import new_passed_report

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging, formal = root / "staging", root / "formal"
            promote_research_artifact(
                staging, formal, new_passed_report(staging)
            )
            path = formal / "reproduce.json"
            payload = load_reproduction_information(str(path))
            self.assertEqual(
                normalize_reproduction_parameters(payload),
                payload["parameters"],
            )
            load_bound_acceptance(str(path), payload, True)
            payload["execution_identity_hash"] = "changed"
            with self.assertRaises(ValueError):
                load_bound_acceptance(str(path), payload, True)

    def test_unknown_research_version_cannot_use_legacy_version_number(self):
        from heretic.artifact_schema import parse_reproduce

        with self.assertRaises(ValueError):
            parse_reproduce(
                {"schema": "cara-research-reproduce-v99", "version": 4}
            )


if __name__ == "__main__":
    unittest.main()
