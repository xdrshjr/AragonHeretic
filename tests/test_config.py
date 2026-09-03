# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026  Philipp Emanuel Weidmann <pew@worldwidemann.com> + contributors

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # ty:ignore[unresolved-import]

from heretic.config import (
    AbliterationMethod,
    RowNormalization,
    ScorerConfig,
    Settings,
    merge_study_settings,
)


def make_settings(values: dict) -> Settings:
    with patch.object(sys, "argv", ["test"]):
        return Settings.model_validate(values)


class ScorerConfigTests(unittest.TestCase):
    def test_accepts_slug_like_instance_name(self) -> None:
        config = ScorerConfig(
            plugin="heretic.scorers.keyword_rate.KeywordRate",
            optimization="minimize",
            instance_name="small-1",
        )
        self.assertEqual(config.instance_name, "small-1")

    def test_rejects_empty_instance_name(self) -> None:
        with self.assertRaises(ValidationError):
            ScorerConfig(
                plugin="heretic.scorers.keyword_rate.KeywordRate",
                optimization="minimize",
                instance_name=" \t",
            )

    def test_rejects_whitespace_in_instance_name(self) -> None:
        for instance_name in ["small name", "small\tname", "small\nname"]:
            with self.subTest(instance_name=instance_name):
                with self.assertRaisesRegex(
                    ValidationError, "whitespace is not allowed"
                ):
                    ScorerConfig(
                        plugin="heretic.scorers.keyword_rate.KeywordRate",
                        optimization="minimize",
                        instance_name=instance_name,
                    )

    def test_rejects_dot_in_instance_name(self) -> None:
        with self.assertRaisesRegex(ValidationError, "'\\.' is not allowed"):
            ScorerConfig(
                plugin="heretic.scorers.keyword_rate.KeywordRate",
                optimization="minimize",
                instance_name="small.name",
            )


class CARAConfigTests(unittest.TestCase):
    def test_default_method_remains_directional(self) -> None:
        settings = make_settings({"model": "org/model"})
        self.assertEqual(settings.abliteration_method, AbliterationMethod.DIRECTIONAL)

    def test_ara_rejects_row_normalization_and_reserved_kwargs(self) -> None:
        with self.assertRaisesRegex(ValidationError, "row_normalization"):
            make_settings(
                {
                    "model": "org/model",
                    "abliteration_method": "ara",
                    "row_normalization": RowNormalization.FULL,
                }
            )
        with self.assertRaisesRegex(ValidationError, "reserved generation"):
            make_settings(
                {"model": "org/model", "generation_kwargs": {"input_ids": []}}
            )

    def test_components_are_deduplicated_and_unknown_values_rejected(self) -> None:
        settings = make_settings(
            {
                "model": "org/model",
                "target_components": ["attn.o_proj", "attn.o_proj"],
            }
        )
        self.assertEqual(settings.target_components, ["attn.o_proj"])
        with self.assertRaisesRegex(ValidationError, "unsupported target"):
            make_settings({"model": "org/model", "target_components": ["other"]})

    def test_runtime_ranges_and_template_keys_are_validated(self) -> None:
        for values in (
            {"ara_capture_batch_size": 0},
            {"ara_softmin_temperature": 0},
            {"chat_template_kwargs": {"tokenize": False}},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                make_settings({"model": "org/model", **values})
        settings = make_settings(
            {
                "model": "org/model",
                "chat_template_kwargs": {"enable_thinking": False},
                "generation_kwargs": {"do_sample": False},
            }
        )
        self.assertFalse(settings.chat_template_kwargs["enable_thinking"])
        self.assertFalse(settings.generation_kwargs["do_sample"])

    def test_resume_keeps_current_run_control_fields(self) -> None:
        stored = make_settings(
            {
                "model": "org/model",
                "seed": 1,
                "n_trials": 120,
                "save_directory": "old",
                "study_checkpoint_dir": "old-checkpoints",
            }
        )
        current = make_settings(
            {
                "model": "ignored/model",
                "seed": 2,
                "n_trials": 200,
                "save_directory": "new",
                "study_checkpoint_dir": "new-checkpoints",
            }
        )
        with patch.object(sys, "argv", ["test"]):
            merged = merge_study_settings(stored, current)
        self.assertEqual((merged.model, merged.seed), ("org/model", 1))
        self.assertEqual(merged.n_trials, 200)
        self.assertEqual(merged.save_directory, "new")
        self.assertEqual(merged.study_checkpoint_dir, "new-checkpoints")

    def test_registered_qwen_configuration_parses(self) -> None:
        path = Path(__file__).parents[1] / "config.qwen38-27b-cara.toml"
        values = tomllib.loads(path.read_text(encoding="utf-8"))
        settings = make_settings(values)
        self.assertEqual(settings.abliteration_method, AbliterationMethod.ARA)
        self.assertEqual(settings.ara_lora_rank, 128)
        self.assertEqual(
            settings.model_commit, "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"
        )
        self.assertEqual(settings.device_map, "balanced")
        self.assertEqual(
            settings.max_memory, {"0": "22GiB", "1": "22GiB", "cpu": "80GiB"}
        )
        self.assertEqual((settings.n_trials, settings.n_startup_trials), (120, 36))
        self.assertEqual(settings.ara_capture_batch_size, 1)
        self.assertEqual(settings.export_strategy.value, "adapter")
        self.assertIsNone(settings.response_prefix)
        self.assertFalse(settings.chat_template_kwargs["enable_thinking"])
        self.assertFalse(settings.generation_kwargs["do_sample"])
        assert settings.model_extra is not None
        markers = settings.model_extra["scorer"]["KeywordRate"]["keyword_markers"]
        self.assertTrue({"抱歉", "我不能", "不便提供"}.issubset(markers))
        with self.assertRaisesRegex(ValidationError, "only supported for ARA"):
            make_settings({**values, "abliteration_method": "directional"})
        with self.assertRaisesRegex(ValidationError, "unique scorer"):
            make_settings({**values, "scorers": [values["scorers"][0]] * 2})

    def test_acceptance_gate_requires_deterministic_pinned_disjoint_data(self) -> None:
        path = Path(__file__).parents[1] / "config.qwen38-27b-cara.toml"
        values = tomllib.loads(path.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValidationError, "do_sample=false"):
            make_settings({**values, "generation_kwargs": {"do_sample": True}})
        with self.assertRaisesRegex(ValidationError, "enable_thinking=false"):
            make_settings({**values, "chat_template_kwargs": {}})
        values["good_prompts"].pop("commit")
        with self.assertRaisesRegex(ValidationError, "pin a commit"):
            make_settings(values)

        values = tomllib.loads(path.read_text(encoding="utf-8"))
        values["scorer"]["KeywordRate"].pop("prompts")
        with self.assertRaisesRegex(ValidationError, "explicitly configure prompts"):
            make_settings(values)

        values = tomllib.loads(path.read_text(encoding="utf-8"))
        values["scorer"]["KeywordRate"]["prompts"]["split"] = "test[:10%]"
        with self.assertRaisesRegex(ValidationError, "overlap"):
            make_settings(values)


if __name__ == "__main__":
    unittest.main()
