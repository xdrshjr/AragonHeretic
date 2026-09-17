# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from heretic.engineering_direct import (
    _check_hardware,
    _load_settings,
    build_parser,
    data_ranges,
    select_trial,
    trial_has_update,
)


def _trial(attempt, *, accepted=True, keywords=0.5, first_kl=0.1, seq_kl=0.1):
    return {
        "attempt": attempt,
        "state": "COMPLETE",
        "events": [{"accepted": accepted}],
        "scores": {
            "keywords": keywords,
            "first_token_kl": first_kl,
            "sequence_kl": seq_kl,
            "log_odds": 0.2,
        },
    }


class EngineeringDirectTests(unittest.TestCase):
    def test_qwen3_template_preserves_model_inventory_and_memory_limits(self):
        root = Path(__file__).resolve().parents[1]
        options = build_parser().parse_args(
            [
                "--model", "/models/Qwen3-1.7B",
                "--config-template", str(root / "config.qwen3-1.7b-ara-v3.toml"),
            ]
        )
        settings = _load_settings(options, Path("test-run"))

        self.assertIsNone(settings.model_commit)
        self.assertEqual(settings.quantization, "none")
        self.assertEqual(settings.device_map, "cuda:0")
        self.assertEqual(settings.max_memory["0"], "22GiB")
        guard = settings.ara_runtime_guard
        self.assertEqual(guard.expected_target_total, 56)
        self.assertEqual(guard.max_cuda_allocated_gib, 22.0)
        self.assertEqual(guard.required_target_devices, ["cuda:0"])
        self.assertEqual(
            [(t.count, t.in_features, t.out_features) for t in guard.targets],
            [(28, 2048, 2048), (28, 6144, 2048)],
        )
        self.assertEqual(settings.ara_v3.sweeps, 2)
        self.assertEqual(settings.ara_v3.proposal_policy, "spectral-backtrack-v1")

    def test_custom_template_accepts_cuda_without_pro6000_restriction(self):
        options = build_parser().parse_args(["--config-template", "custom.toml"])
        torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True))
        with patch.dict(sys.modules, {"torch": torch}):
            _check_hardware(options)

    def test_custom_template_rejects_missing_cuda(self):
        options = build_parser().parse_args(["--config-template", "custom.toml"])
        torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
        with patch.dict(sys.modules, {"torch": torch}):
            with self.assertRaisesRegex(ValueError, "CUDA GPU is required"):
                _check_hardware(options)

    def test_settings_load_with_engineering_cli_arguments(self):
        arguments = [
            "--model",
            "/models/Qwen3.8-27B",
            "--run-root",
            "/heretic-runs",
            "--seed",
            "42",
            "--fit-samples",
            "96",
            "--monitor-samples",
            "64",
            "--development-samples",
            "100",
            "--trials",
            "24",
            "--max-hours",
            "48",
        ]
        options = build_parser().parse_args(arguments)
        argv = ["engineering_direct.py", *arguments]
        with patch.object(sys, "argv", argv):
            settings = _load_settings(options, Path("test-run"))
            self.assertIs(sys.argv, argv)

        self.assertEqual(settings.model, str(options.model.resolve()))
        self.assertEqual(settings.device_map, "cuda:0")
        self.assertEqual(settings.seed, 42)
        self.assertEqual(settings.ara_v3.proposal_policy, "spectral-backtrack-v1")
        self.assertEqual(
            settings.study_checkpoint_dir, str(Path("test-run") / "trials")
        )

    def test_settings_restore_cli_arguments_after_validation_error(self):
        root = Path(__file__).resolve().parents[1]
        template = root / "config.qwen38-27b-cara-v3-96.toml"
        invalid = template.read_text(encoding="utf-8").replace(
            "ara_lora_rank = 128", "ara_lora_rank = 0"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.toml"
            path.write_text(invalid, encoding="utf-8")
            arguments = ["--config-template", str(path)]
            options = build_parser().parse_args(arguments)
            argv = ["engineering_direct.py", *arguments]
            with patch.object(sys, "argv", argv):
                with self.assertRaises(ValidationError):
                    _load_settings(options, Path("test-run"))
                self.assertIs(sys.argv, argv)

    def test_default_cli_is_small_direct_run(self):
        options = build_parser().parse_args([])

        self.assertEqual(options.fit_samples, 8)
        self.assertEqual(options.monitor_samples, 8)
        self.assertEqual(options.development_samples, 20)
        self.assertEqual(options.trials, 1)

    def test_data_ranges_are_disjoint_and_exact(self):
        ranges = data_ranges(8, 12, 20)

        self.assertEqual(
            ranges,
            {
                "fit": (0, 8),
                "monitor": (8, 20),
                "development": (20, 40),
            },
        )

    def test_data_ranges_reject_source_overflow(self):
        with self.assertRaisesRegex(ValueError, "exceeds"):
            data_ranges(200, 100, 101)

    def test_selection_requires_an_effective_update(self):
        unchanged = _trial(0, accepted=False)

        self.assertFalse(trial_has_update(unchanged))
        with self.assertRaisesRegex(RuntimeError, "accepted adapter update"):
            select_trial([unchanged])

    def test_selection_prefers_kl_guarded_candidate(self):
        lower_keywords_but_unsafe = _trial(0, keywords=0.1, first_kl=0.2, seq_kl=0.2)
        guarded = _trial(1, keywords=0.4, first_kl=0.1, seq_kl=0.1)

        selected = select_trial([lower_keywords_but_unsafe, guarded])

        self.assertEqual(selected["attempt"], 1)


if __name__ == "__main__":
    unittest.main()
