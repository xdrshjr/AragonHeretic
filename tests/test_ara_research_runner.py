# SPDX-License-Identifier: AGPL-3.0-or-later

import tempfile
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from heretic.ara_research_runner import (
    NoResearchCandidate,
    StudyBudget,
    paired_parameters,
    run_search,
    replay_locked_candidate,
    select_research_candidate,
)
from heretic.ara_research_schema import (
    CandidateLock,
    digest,
    file_digest,
    write_json,
    read_json,
)


def trial_scores(keywords=0.05):
    return {
        "keywords": keywords,
        "baseline_keywords": 0.8,
        "first_token_kl": 0.02,
        "log_odds": -0.5,
    }


def prepare_orphan_fixture(root):
    settings = SimpleNamespace(
        reproduce=False,
        study_checkpoint_dir=root,
        seed=42,
        ara_v3=SimpleNamespace(method_id="S2"),
        model_dump=lambda **_: {"seed": 42},
    )
    protocol = {"protocol_hash": "protocol", "pilot": False}
    identity = {
        "protocol_hash": "protocol",
        "method_id": "S2",
        "seed": 42,
        "settings_hash": digest({"seed": 42}),
    }
    write_json(
        root / "study.json",
        {
            **identity,
            "study_hash": digest(identity),
            "trials": [{"attempt": 0, "state": "RUNNING"}],
        },
    )
    write_json(
        root / "budget.json",
        {
            "sessions": [
                {
                    "start": 1.0,
                    "stop": 2.0,
                    "deadline": 2.0,
                    "devices": 2,
                }
            ]
        },
    )
    return settings, protocol


class RunnerTests(unittest.TestCase):
    def test_initial_finalize_error_allows_corrected_request(self):
        from heretic.ara_research_runner import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "finalize-result.json"
            argv = [
                "runner",
                "--config",
                "unused",
                "--run-dir",
                directory,
                "--phase",
                "finalize",
            ]
            valid = {
                "phase": "finalize",
                "research_status": "passed",
                "inputs_hash": "frozen-inputs",
            }
            with (
                patch.object(sys, "argv", argv),
                patch(
                    "heretic.ara_research_runner._dispatch_phase",
                    side_effect=[ValueError("缺少标签文件"), (valid, 0)],
                ),
            ):
                with self.assertRaises(SystemExit) as failed:
                    main()
                self.assertEqual(failed.exception.code, 2)
                self.assertFalse(path.exists())
                self.assertEqual(
                    len(list((root / "phase-errors").glob("finalize-*.json"))),
                    1,
                )
                with self.assertRaises(SystemExit) as success:
                    main()
                self.assertEqual(success.exception.code, 0)
            self.assertEqual(read_json(path), valid)

    def test_finalize_error_does_not_overwrite_frozen_phase_result(self):
        from heretic.ara_research_runner import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "finalize-result.json"
            write_json(path, {"phase": "finalize", "research_status": "passed"})
            original = path.read_bytes()
            argv = [
                "runner",
                "--config",
                "unused",
                "--run-dir",
                directory,
                "--phase",
                "finalize",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch(
                    "heretic.ara_research_runner._dispatch_phase",
                    side_effect=ValueError("冻结标注已改变"),
                ),
            ):
                with self.assertRaises(SystemExit) as result:
                    main()
            self.assertEqual(result.exception.code, 2)
            self.assertEqual(path.read_bytes(), original)
            errors = list((root / "phase-errors").glob("finalize-*.json"))
            self.assertEqual(len(errors), 1)
            self.assertEqual(read_json(errors[0])["reason"], "冻结标注已改变")

    def test_snapshot_restore_failure_stops_search_after_terminal_record(self):
        from heretic.ara_refinement_capture import SnapshotRestoreError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with StudyBudget(root / "budget.json") as budget:
                with patch(
                    "heretic.ara_research_runner._sample_attempt",
                    return_value=(paired_parameters(42, 0), None),
                ):
                    context = {
                        "run_dir": root,
                        "budget": budget,
                        "identity": {"method_id": "S2", "seed": 42},
                        "apply": lambda *_: self._raise_restore_error(),
                    }
                    with self.assertRaises(SnapshotRestoreError):
                        run_search(context)
            trials = read_json(root / "study.json")["trials"]
            self.assertEqual(len(trials), 1)
            self.assertEqual(trials[0]["state"], "FAIL")
            self.assertEqual(
                trials[0]["failure_category"], "SnapshotRestoreError"
            )

    @staticmethod
    def _raise_restore_error():
        from heretic.ara_refinement_capture import SnapshotRestoreError

        raise SnapshotRestoreError("恢复因子失败")

    def test_exhausted_resume_finalizes_orphan_before_loading_model(self):
        from heretic.ara_research_runner import (
            ResearchBudgetExceeded,
            run_from_settings,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings, protocol = prepare_orphan_fixture(root)
            with (
                patch(
                    "heretic.research_protocol.prepare_protocol",
                    return_value=protocol,
                ),
                patch(
                    "heretic.ara_refinement_config.research_phase_limit",
                    return_value=1.0,
                ),
                patch("heretic.ara_research_runner._runtime_context") as load,
            ):
                with self.assertRaises(ResearchBudgetExceeded):
                    run_from_settings(settings, "search")
            load.assert_not_called()
            trials = read_json(root / "study.json")["trials"]
            self.assertEqual(len(trials), 1)
            self.assertEqual(trials[0]["state"], "FAIL")
            self.assertTrue(trials[0]["interrupted"])

    def test_subprocess_failures_write_phase_result_and_exit_three(self):
        from heretic.ara_research_runner import main

        failures = [
            subprocess.CalledProcessError(7, ["judge"]),
            subprocess.TimeoutExpired(["judge"], 2.0),
        ]
        for phase in ("search", "audit"):
            for error in failures:
                with self.subTest(phase=phase, error=type(error).__name__):
                    with tempfile.TemporaryDirectory() as directory:
                        args = [
                            "runner",
                            "--config",
                            "unused.toml",
                            "--run-dir",
                            directory,
                            "--phase",
                            phase,
                        ]
                        with (
                            patch.object(sys, "argv", args),
                            patch(
                                "heretic.ara_research_runner._dispatch_phase",
                                side_effect=error,
                            ),
                        ):
                            with self.assertRaises(SystemExit) as result:
                                main()
                        self.assertEqual(result.exception.code, 3)
                        report = read_json(
                            Path(directory) / f"{phase}-result.json"
                        )
                        self.assertEqual(report["research_status"], "failed")
                        self.assertEqual(
                            report["failure_category"], type(error).__name__
                        )

    def test_nested_budget_keeps_outer_deadline_and_kills_only_descendants(
        self,
    ):
        import psutil

        script = """
import subprocess, sys, time
from pathlib import Path
from heretic.ara_research_runner import StudyBudget
from heretic.ara_research_schema import write_json
root = Path(sys.argv[1])
with StudyBudget(root / 'outer.json', 1.0):
    child = subprocess.Popen([
        sys.executable, '-c', 'import time; time.sleep(30)',
    ])
    write_json(root / 'child.json', {'pid': child.pid})
    with StudyBudget(root / 'inner.json', 10.0):
        pass
    time.sleep(30)
"""
        with tempfile.TemporaryDirectory() as directory:
            unrelated = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"]
            )
            try:
                worker = subprocess.run(
                    [sys.executable, "-c", script, directory],
                    timeout=15,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(worker.returncode, 3, worker.stderr)
                self.assertIsNone(unrelated.poll())
                child_pid = read_json(Path(directory) / "child.json")["pid"]
                if psutil.pid_exists(child_pid):
                    self.assertEqual(
                        psutil.Process(child_pid).status(), psutil.STATUS_ZOMBIE
                    )
                report = read_json(Path(directory) / "budget-timeout.json")
                self.assertIn(child_pid, report["terminated_descendants"])
            finally:
                unrelated.kill()
                unrelated.wait(timeout=5)

    def test_pilot_and_ablation_reject_unregistered_member(self):
        from heretic.ara_refinement_config import research_phase_limit

        for method, phase, name in (
            ("S2", "pilot", "pilot"),
            ("A1", "search", "ablation"),
        ):
            settings = SimpleNamespace(
                seed=42, ara_v3=SimpleNamespace(method_id=method)
            )
            protocol = {
                "phase_budgets": {
                    name: {
                        "members": ["S1-42"],
                        "max_wallclock_seconds": 10,
                        "max_gpu_hours": 1,
                    }
                }
            }
            with self.assertRaisesRegex(ValueError, "成员"):
                research_phase_limit(settings, protocol, phase)

    def test_pilot_budget_is_shared_across_run_directories(self):
        from heretic.research_budget import phase_budget_path
        from heretic.ara_research_runner import ResearchBudgetExceeded

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(
                ara_v3=SimpleNamespace(
                    method_id="S2",
                    protocol_manifest=str(root / "protocol.json"),
                )
            )
            protocol = {"protocol_hash": "protocol"}
            first = phase_budget_path(settings, protocol, "pilot", root / "a")
            settings.ara_v3.method_id = "S1"
            second = phase_budget_path(settings, protocol, "pilot", root / "b")
            self.assertEqual(first, second)
            now = [10.0]
            with StudyBudget(first, 5.0, clock=lambda: now[0]):
                now[0] += 5.0
            with self.assertRaises(ResearchBudgetExceeded):
                with StudyBudget(second, 5.0, clock=lambda: now[0]):
                    self.fail("其他成员不能重置阶段预算")

    def test_replay_resume_reuses_complete_evidence_and_latches_failure(self):
        from heretic.ara_refinement_capture import tensor_identity
        import torch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            factors = {"module.A": torch.ones(2, 2)}
            path = root / "factors.pt"
            torch.save(factors, path)
            evidence = {
                "final_snapshot_path": path.as_posix(),
                "events": [],
                "final_snapshot_hash": tensor_identity(factors),
                "scores": {**trial_scores(), "sequence_kl": 0.01},
            }
            lock = CandidateLock(
                protocol_hash="protocol",
                study_hash="study",
                method_id="S2",
                seed=42,
                attempt=0,
                parameters=paired_parameters(42, 0),
                shortlist=[0],
                selection_rule_hash="selection",
                final_snapshot_path=path.as_posix(),
                final_snapshot_hash=tensor_identity(factors),
                score_evidence_hash="scores",
                eligibility="qualified",
            )
            study = {"candidate_lock": lock.model_dump(), "trials": [evidence]}
            for phase in ("replay-1", "replay-2", "third-apply"):
                write_json(root / phase / "0" / "trial.json", evidence)
            with StudyBudget(root / "budget.json") as budget:
                context = {
                    "run_dir": root,
                    "budget": budget,
                    "apply": lambda *args: self.fail("不得重复执行"),
                }
                self.assertEqual(
                    len(replay_locked_candidate(context, study)), 3
                )
                with patch(
                    "heretic.ara_research_runner.compare_replay",
                    side_effect=ValueError("因子漂移"),
                ):
                    with self.assertRaises(ValueError):
                        replay_locked_candidate(context, study)
                self.assertTrue((root / "replay-failure.json").exists())
                with self.assertRaisesRegex(ValueError, "曾重放失败"):
                    replay_locked_candidate(context, study)

    def test_anchors_are_distinct_and_random_stage_is_deterministic(self):
        anchors = [
            digest(paired_parameters(42, i).model_dump()) for i in range(4)
        ]
        self.assertEqual(len(set(anchors)), 4)
        self.assertEqual(paired_parameters(42, 8), paired_parameters(42, 8))
        self.assertNotEqual(paired_parameters(42, 8), paired_parameters(43, 8))

    def test_budget_charges_across_multiple_launches(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [100.0]
            path = Path(directory) / "budget.json"
            with StudyBudget(path, 30, clock=lambda: now[0]) as budget:
                now[0] += 10
                self.assertEqual(budget.elapsed(), 10.0)
            with StudyBudget(path, 30, clock=lambda: now[0]) as budget:
                now[0] += 5
                self.assertEqual(budget.elapsed(), 15.0)

    def test_shortlist_filters_hard_constraints_before_top_three(self):
        study = {
            "trials": [
                {
                    "attempt": i,
                    "state": "COMPLETE",
                    "scores": trial_scores(),
                    "development_semantics": {},
                }
                for i in range(24)
            ]
        }
        for row in study["trials"]:
            row["scores"]["first_token_kl"] = 0.2
        with self.assertRaises(NoResearchCandidate):
            select_research_candidate(study, {})

    def test_search_has_exact_24_terminal_attempts_and_resumes_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.bin"
            snapshot.write_bytes(b"CPU fixture")
            calls = []

            def apply(parameters, attempt, phase):
                calls.append(attempt)
                return {
                    "attempt": attempt,
                    "state": "COMPLETE",
                    "scores": trial_scores(),
                    "final_snapshot_path": snapshot.as_posix(),
                    "snapshot_file_hash": file_digest(snapshot),
                    "final_snapshot_hash": "tensor",
                }

            semantics = {
                "semantic_refusal_rate": 0.02,
                "invalid_rate": 0.0,
                "valid_rate": 1.0,
                "sequence_kl": 0.01,
            }
            protocol = {
                "protocol_hash": "protocol",
                "judge_identity": {"rubric": "fixed"},
            }
            with StudyBudget(root / "budget.json") as budget:
                context = {
                    "run_dir": root,
                    "identity": {"method_id": "S2", "seed": 42},
                    "apply": apply,
                    "semantic": lambda row: semantics,
                    "protocol": protocol,
                    "budget": budget,
                }
                result = run_search(context)
                self.assertEqual(len(result["trials"]), 24)
                self.assertEqual(result["candidate_lock"]["attempt"], 0)
                self.assertEqual(result["frozen_shortlist"], [0, 1, 2])
                run_search(context)
                self.assertEqual(len(calls), 24)


if __name__ == "__main__":
    unittest.main()
