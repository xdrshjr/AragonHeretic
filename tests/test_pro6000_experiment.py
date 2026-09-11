# SPDX-License-Identifier: AGPL-3.0-or-later
"""验证全量选题、原生搜索复用、冻结身份和恢复边界。"""

import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from heretic.ara_research_runner import run_search
from heretic.ara_research_schema import (
    PROTOCOL_SCHEMA,
    digest,
    exclusive_lock,
    file_digest,
    read_json,
    write_json,
)
from heretic.pro6000_experiment import (
    choose_experiment_candidate,
    main,
    validate_run_binding,
    verify_completed_result,
)
from heretic.pro6000_launch import freeze_source
from heretic.pro6000_prepare import (
    ROLE_SIZES,
    SCOPE,
    check_hardware,
    split_full_rows,
)
from heretic.research_budget import StudyBudget
from heretic.research_protocol import prepare_protocol


def data_fixture(count=416):
    rows = [
        {
            "row_index": index,
            "text": f"真实来源的测试题 {index}",
            "source": "fixture",
            "revision": "fixed",
            "split": "train",
            "system": "原有系统提示",
        }
        for index in range(count)
    ]
    previous = {
        "fit": copy.deepcopy(rows[:8]),
        "monitor": copy.deepcopy(rows[8:16]),
        "mechanism-development": copy.deepcopy(rows[16:60]),
        "development": copy.deepcopy(rows[60:160]),
    }
    return rows, previous


def scores(keywords=0.05, kl=0.02):
    return {
        "keywords": keywords,
        "first_token_kl": kl,
        "baseline_keywords": 0.8,
        "log_odds": -0.5,
    }


class PreparationTests(unittest.TestCase):
    def test_full_roles_preserve_original_prompts_and_never_overlap(self):
        rows, previous = data_fixture()
        previous["development"][0]["system"] = "冻结的独立系统提示"
        result = split_full_rows(rows, previous)
        for role, count in ROLE_SIZES.items():
            self.assertEqual(len(result[role]), count)
            self.assertEqual(
                result[role][: len(previous[role])], previous[role]
            )
        selected = [
            row["row_index"] for role in result.values() for row in role
        ]
        self.assertEqual(len(set(selected)), 400)
        self.assertEqual(set(selected), set(range(400)))

    def test_changed_source_or_insufficient_unique_data_fails(self):
        rows, previous = data_fixture(399)
        with self.assertRaisesRegex(ValueError, "不足"):
            split_full_rows(rows, previous)
        rows, previous = data_fixture()
        rows[60]["revision"] = "changed"
        with self.assertRaisesRegex(ValueError, "不一致"):
            split_full_rows(rows, previous)

    def test_original_cross_role_duplicate_fails(self):
        rows, previous = data_fixture()
        previous["monitor"][0] = previous["fit"][0]
        with self.assertRaisesRegex(ValueError, "重复"):
            split_full_rows(rows, previous)

    def test_wrong_or_busy_gpu_fails_before_loading(self):
        for output in ("RTX 4090, 24564, 0", "RTX PRO 6000, 97887, 90000"):
            with patch("subprocess.check_output", return_value=output):
                with self.assertRaises(ValueError):
                    check_hardware()
        with patch(
            "subprocess.check_output", return_value="RTX PRO 6000, 97887, 0"
        ):
            self.assertEqual(check_hardware()["used_mib"], 0)

    def test_experiment_protocol_cannot_enter_formal_research(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "protocol.json"
            protocol = {
                "schema_version": PROTOCOL_SCHEMA,
                "execution_scope": SCOPE,
                "roles": {},
            }
            write_json(path, {**protocol, "protocol_hash": digest(protocol)})
            settings = SimpleNamespace(
                ara_v3=SimpleNamespace(protocol_manifest=path)
            )
            with self.assertRaisesRegex(ValueError, "对应实验入口"):
                prepare_protocol(settings)
            with self.assertRaisesRegex(ValueError, "必需的开发角色"):
                prepare_protocol(settings, experiment=True)

    def test_budget_and_seed_cannot_change_on_resume(self):
        run = {
            "hours": 48,
            "seed": 42,
            "model": "model",
            "source_revision": "git",
        }
        settings = SimpleNamespace(
            ara_v3=SimpleNamespace(method_id="S2"), seed=42, model="model"
        )
        protocol = {
            "source_revision": "git",
            "phase_budgets": {
                "experiment": {
                    "max_wallclock_seconds": 48 * 3600,
                    "devices": 1,
                    "members": ["S2-42"],
                }
            },
        }
        validate_run_binding(run, settings, protocol)
        with self.assertRaisesRegex(ValueError, "预算"):
            validate_run_binding({**run, "hours": 96}, settings, protocol)
        settings.seed = 43
        with self.assertRaisesRegex(ValueError, "种子"):
            validate_run_binding(run, settings, protocol)


class SearchTests(unittest.TestCase):
    def test_native_24_trial_search_and_resume_need_no_semantic_judge(self):
        calls = []

        def apply(parameters, attempt, phase):
            calls.append(attempt)
            return {
                "state": "COMPLETE",
                "scores": scores(),
                "snapshot_file_hash": f"snapshot-{attempt}",
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with StudyBudget(root / "budget.json", devices=1) as budget:
                context = {
                    "run_dir": root,
                    "budget": budget,
                    "apply": apply,
                    "identity": {"method_id": "S2", "seed": 42},
                }
                study = run_search(
                    context, finalize=choose_experiment_candidate
                )
                replay = run_search(
                    context, finalize=choose_experiment_candidate
                )
            self.assertEqual(calls, list(range(24)))
            self.assertEqual(study, replay)
            self.assertTrue(
                all(row["rng_state"] is None for row in study["trials"][:12])
            )
            self.assertTrue(
                all(row["rng_state"] for row in study["trials"][12:])
            )
            self.assertEqual(
                study["experiment_candidate"]["eligibility"], "comparison_only"
            )
            self.assertNotIn("candidate_lock", study)
            self.assertEqual(study["research_status"], "not_run")
            self.assertEqual(
                read_json(root / "budget.json")["sessions"][0]["devices"], 1
            )

    def test_numeric_gate_precedes_keyword_ranking(self):
        study = {
            "trials": [
                {
                    "attempt": 0,
                    "state": "COMPLETE",
                    "scores": scores(0, kl=5),
                    "snapshot_file_hash": "infeasible",
                },
                {
                    "attempt": 1,
                    "state": "COMPLETE",
                    "scores": scores(),
                    "snapshot_file_hash": "feasible",
                },
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "study.json"
            choose_experiment_candidate({}, study, path)
            self.assertEqual(study["experiment_candidate"]["attempt"], 1)
            study["trials"].pop()
            choose_experiment_candidate({}, study, path)
            self.assertFalse(
                study["experiment_candidate"]["numeric_gate_passed"]
            )

    def test_single_gpu_checkpoint_accounting(self):
        from heretic.ara_research_runner import _run_attempt

        with tempfile.TemporaryDirectory() as directory:
            context = {
                "budget": SimpleNamespace(devices=1, elapsed=lambda: 3600),
                "apply": lambda *_: {"state": "COMPLETE", "scores": scores()},
            }
            study = {"seed": 42, "trials": []}
            _run_attempt(context, study, Path(directory) / "study.json", 0)
            self.assertEqual(study["trials"][0]["completed_gpu_hours"], 1.0)

    def test_remaining_snapshot_reserve_keeps_formal_default(self):
        from heretic.ara_refinement_capture import preflight_snapshot_storage
        from test_ara_refinement_capture import tiny_model

        model = tiny_model()
        with tempfile.TemporaryDirectory() as directory:
            formal = preflight_snapshot_storage(model, directory)
            last = preflight_snapshot_storage(model, directory, snapshots=1)
            self.assertGreater(
                formal["required_bytes"], last["required_bytes"] * 31
            )
            with self.assertRaises(ValueError):
                preflight_snapshot_storage(model, directory, snapshots=0)


class RecoveryTests(unittest.TestCase):
    def test_finished_export_is_verified_without_loading_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = root / "adapter.safetensors"
            adapter.write_bytes(b"adapter fixture")
            result = {
                "status": "completed",
                "adapter": {
                    "path": str(root),
                    "file_hashes": {adapter.name: file_digest(adapter)},
                },
                "snapshot": {
                    "path": str(adapter),
                    "sha256": file_digest(adapter),
                },
            }
            write_json(root / "experiment-result.json", result)
            self.assertEqual(verify_completed_result(root), result)
            adapter.write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "损坏"):
                verify_completed_result(root)

    def test_competing_worker_cannot_overwrite_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "status.json", {"stage": "search"})
            with (
                exclusive_lock(root / "worker.lock"),
                patch.object(sys, "argv", ["worker", "--run-dir", str(root)]),
                patch("signal.signal"),
            ):
                with self.assertRaises(SystemExit) as stopped:
                    main()
            self.assertEqual(stopped.exception.code, 2)
            self.assertEqual(
                read_json(root / "status.json"), {"stage": "search"}
            )
            self.assertFalse((root / "exit.json").exists())

    def test_freeze_uses_committed_content_even_after_worktree_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, output = root / "project", root / "run"
            project.mkdir()
            output.mkdir()
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            source = project / "src/heretic/pro6000_experiment.py"
            source.parent.mkdir(parents=True)
            source.write_text("# committed\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(project), "add", "."], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(project),
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                check=True,
            )
            source.write_text("# uncommitted\n", encoding="utf-8")
            revision = freeze_source(project, output)
            self.assertEqual(len(revision), 40)
            frozen = output / "source/src/heretic/pro6000_experiment.py"
            self.assertEqual(
                frozen.read_text(encoding="utf-8"), "# committed\n"
            )


if __name__ == "__main__":
    unittest.main()
