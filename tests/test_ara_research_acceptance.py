# SPDX-License-Identifier: AGPL-3.0-or-later

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from heretic.ara_research_acceptance import (
    combine_states,
    development_gate,
    engineering_status,
    promote_research_artifact,
)
from heretic.ara_research_schema import (
    ACCEPTANCE_SCHEMA,
    RefinementParameters,
    validate_research_parameters,
    write_json,
)


class ResearchAcceptanceTests(unittest.TestCase):
    def test_finalize_rejects_missing_ledger_roles(self):
        from heretic.ara_research_acceptance import _verify_ledger_bindings
        from test_research_audit import evaluation_fixture

        with self.assertRaisesRegex(ValueError, "账本角色"):
            _verify_ledger_bindings(evaluation_fixture(), {"ledgers": {}})

    def test_finalize_requires_complete_consumption_for_each_evidence(self):
        from heretic.ara_research_acceptance import _verify_ledger_bindings
        from heretic.ara_research_schema import file_digest
        from heretic.research_audit import _open_ledger
        from test_research_audit import evaluation_fixture

        plan = evaluation_fixture()
        with tempfile.TemporaryDirectory() as directory:
            results = {
                "ledgers": {},
                "members": {row["member_id"]: {} for row in plan["members"]},
            }
            for role in plan["audit_manifests"]:
                path, _ = _open_ledger(plan, role, Path(directory))
                results["ledgers"][role] = {
                    "path": str(path),
                    "sha256": file_digest(path),
                }
            results["members"]["B0"]["research-audit.good"] = {
                "evidence": {"is_complete": True}
            }
            with self.assertRaisesRegex(ValueError, "未完成"):
                _verify_ledger_bindings(plan, results)

    def test_finalize_accepts_complete_bound_ledger_evidence(self):
        from heretic.ara_research_acceptance import _verify_ledger_bindings
        from heretic.ara_research_schema import file_digest
        from heretic.research_audit import AuditContext, evaluate_audit_member
        from test_research_audit import evaluation_fixture

        plan = evaluation_fixture()
        results = {
            "ledgers": {},
            "members": {row["member_id"]: {} for row in plan["members"]},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for role, manifest in plan["audit_manifests"].items():
                context = AuditContext(
                    root / "ledgers",
                    root / "outputs",
                    role,
                    lambda _: {"status": "passed", "audit_accessed": False},
                    lambda *_: {"is_complete": True},
                )
                for member in plan["members"]:
                    if member["availability"] == "available":
                        results["members"][member["member_id"]][role] = (
                            evaluate_audit_member(plan, member, context)
                        )
                path = context.ledger_root / f"{manifest}.json"
                results["ledgers"][role] = {
                    "path": str(path),
                    "sha256": file_digest(path),
                }
            _verify_ledger_bindings(plan, results)

    def test_member_finalize_recovers_after_promotion_before_aggregate(self):
        from heretic.ara_research_acceptance import _finalize_member
        from heretic.ara_research_schema import digest, file_digest
        from test_research_audit import evaluation_fixture

        plan = evaluation_fixture()
        member = next(m for m in plan["members"] if m["member_id"] == "S2-42")
        lock = member["candidate_lock"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            write_json(staging / "protocol.json", {"protocol_hash": "protocol"})
            write_json(
                staging / "study.json",
                {
                    "candidate_lock": lock,
                    "trials": [],
                    "effect_status": "passed",
                },
            )
            core = {p.name: file_digest(p) for p in staging.iterdir()}
            stage = {
                "core_hashes": core,
                "model_fingerprint": "model",
                "candidate_lock_hash": digest(lock),
            }
            write_json(staging / "staging.json", stage)
            member.update(
                staging_path=str(staging),
                staging_manifest_hash=file_digest(staging / "staging.json"),
            )
            report = _passed_report(lock, core)
            with (
                patch(
                    "heretic.ara_research_acceptance.finalize_research_report",
                    return_value=report,
                ),
                patch(
                    "heretic.ara_research_acceptance._member_audit_input",
                    return_value={},
                ),
            ):
                first = _finalize_member(
                    plan, member, root, {"ledgers": {}}, {}
                )
                self.assertFalse(staging.exists())
                self.assertFalse((root / "finalize-result.json").exists())
                second = _finalize_member(
                    plan, member, root, {"ledgers": {}}, {}
                )
                self.assertEqual(first, second)
                changed = {**report, "input_hashes": {"labels": "修订"}}
                with (
                    patch(
                        "heretic.ara_research_acceptance.finalize_research_report",
                        return_value=changed,
                    ),
                    self.assertRaises(ValueError),
                ):
                    _finalize_member(plan, member, root, {"ledgers": {}}, {})

    def test_v3_artifact_chain_rejects_parameter_and_core_tampering(self):
        from test_research_audit import evaluation_fixture
        from heretic.ara_research_acceptance import (
            verify_research_artifact_graph,
        )
        from heretic.ara_research_schema import file_digest, read_json

        lock = next(
            row["candidate_lock"]
            for row in evaluation_fixture()["members"]
            if row["member_id"] == "S2-42"
        )
        with tempfile.TemporaryDirectory() as directory:
            staging, formal = (
                Path(directory) / "staging",
                Path(directory) / "formal",
            )
            staging.mkdir()
            (staging / "adapter.bin").write_bytes(b"artifact-chain fixture")
            report = {
                "schema_version": ACCEPTANCE_SCHEMA,
                "status": "passed",
                "required_level": "recovery",
                "eligibility": "qualified",
                "engineering_status": "passed",
                "effect_status": "passed",
                "ability_status": "passed",
                "ability": {"status": "passed"},
                "selected_candidate": lock,
                "audit_plan_hash": "plan",
                "core_hashes": {
                    "adapter.bin": file_digest(staging / "adapter.bin")
                },
                "metrics": {
                    side: {
                        "n": 1,
                        "k": 0,
                        "status": "complete",
                        "missing": [],
                        "refusal_rate": 0.0,
                    }
                    for side in ("good", "bad")
                },
            }
            promote_research_artifact(staging, formal, report)
            verify_research_artifact_graph(formal)
            original = read_json(formal / "reproduce.json")
            changed = read_json(formal / "reproduce.json")
            changed["parameters"]["payload"]["margin"] *= 1.1
            write_json(formal / "reproduce.json", changed)
            with self.assertRaises(ValueError):
                verify_research_artifact_graph(formal)
            write_json(formal / "reproduce.json", original)
            (formal / "adapter.bin").write_bytes(b"corrupted")
            with self.assertRaises(ValueError):
                verify_research_artifact_graph(formal)

    def test_offline_finalize_does_not_load_model_and_reports_missing_audit(
        self,
    ):
        from unittest.mock import patch
        from heretic.ara_research_acceptance import finalize_research_report

        artifacts = {
            "protocol": {
                "protocol_hash": "protocol",
                "required_level": "statistical_extreme",
                "sampling": {"inference_scope": "iid_population"},
                "preparation_access_log_hash": "preparation",
            },
            "candidate_lock": {
                "protocol_hash": "protocol",
                "eligibility": "qualified",
            },
            "audit_plan_hash": "plan",
            "audit_ledger_hash": "ledger",
            "core_hashes": {},
            "development_status": "passed",
            "trials": [{"attempt": i, "state": "COMPLETE"} for i in range(24)],
            "integrity": {
                key: "passed"
                for key in (
                    "data_preflight",
                    "transaction_restore",
                    "replay",
                    "independent_reload",
                    "artifact_chain",
                )
            },
        }
        with patch(
            "heretic.model.Model", side_effect=AssertionError("模型调用")
        ):
            report = finalize_research_report(
                artifacts, {"plan_hash": "plan"}, {}
            )
            artifacts["execution_status"] = "failed"
            failed = finalize_research_report(
                artifacts, {"plan_hash": "plan"}, {}
            )
        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(report["engineering_status"], "passed")
        self.assertEqual(report["ability_status"], "inconclusive")

    def test_empty_gate_is_not_vacuously_passed(self):
        self.assertEqual(combine_states([]), "inconclusive")

    def test_known_failure_precedes_missing_semantic_labels(self):
        from heretic.ara_research_acceptance import _side_status

        states = _side_status(
            {"status": "inconclusive"},
            {
                "keywords": 0.8,
                "baseline_keywords": 0.9,
                "first_token_kl": 0.3,
                "sequence_kl": 0.3,
            },
            "bad",
            {"inference_scope": "iid_population"},
        )
        self.assertEqual(states[0], "failed")

    def test_failure_precedes_missing_evidence(self):
        self.assertEqual(combine_states(["failed", "inconclusive"]), "failed")
        self.assertEqual(combine_states(["passed", "not_run"]), "inconclusive")
        self.assertEqual(combine_states(["passed", "passed"]), "passed")

    def test_zero_baseline_does_not_pass_relative_drop(self):
        scores = {
            "keywords": 0.0,
            "baseline_keywords": 0.0,
            "first_token_kl": 0.0,
            "log_odds": -2.0,
        }
        self.assertFalse(development_gate(scores))

    def test_24_attempt_gate_rejects_legacy_120_trial_budget(self):
        integrity = {
            name: "passed"
            for name in (
                "data_preflight",
                "transaction_restore",
                "replay",
                "independent_reload",
                "artifact_chain",
            )
        }
        trials = [{"attempt": i, "state": "COMPLETE"} for i in range(24)]
        self.assertEqual(engineering_status(trials, integrity), "passed")
        trials[-1]["state"] = "FAIL"
        self.assertEqual(engineering_status(trials, integrity), "passed")
        trials[-2]["state"] = "FAIL"
        self.assertEqual(engineering_status(trials, integrity), "failed")

    def test_v3_parameter_version_is_strict(self):
        from heretic.ara_research_runner import paired_parameters

        envelope = paired_parameters(42, 0).envelope()
        self.assertIsInstance(
            validate_research_parameters(envelope), RefinementParameters
        )
        envelope["objective_version"] = "trajectory-v2"
        with self.assertRaises(ValueError):
            validate_research_parameters(envelope)

    def test_comparison_only_and_inconclusive_cannot_promote(self):
        report = {
            "schema_version": ACCEPTANCE_SCHEMA,
            "status": "inconclusive",
            "required_level": "statistical_extreme",
            "eligibility": "qualified",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                promote_research_artifact(root, root / "formal", report)
            self.assertFalse((root / "formal").exists())

    def test_immutable_report_is_idempotent_but_rejects_changed_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            write_json(path, {"labels_hash": "one"}, immutable=True)
            write_json(path, {"labels_hash": "one"}, immutable=True)
            with self.assertRaises(ValueError):
                write_json(path, {"labels_hash": "two"}, immutable=True)


def _passed_report(lock, core):
    return {
        "schema_version": ACCEPTANCE_SCHEMA,
        "status": "passed",
        "required_level": "recovery",
        "eligibility": "qualified",
        "engineering_status": "passed",
        "effect_status": "passed",
        "ability_status": "passed",
        "ability": {"status": "passed"},
        "selected_candidate": lock,
        "audit_plan_hash": "plan",
        "core_hashes": core,
        "metrics": {
            side: {
                "n": 1,
                "k": 0,
                "status": "complete",
                "missing": [],
                "refusal_rate": 0.0,
            }
            for side in ("good", "bad")
        },
    }


def new_passed_report(root):
    """生成可验证新版文件链的小型夹具，无模型或研究效果声明。"""
    from test_research_audit import evaluation_fixture
    from heretic.ara_research_schema import file_digest

    member = next(
        m for m in evaluation_fixture()["members"] if m["member_id"] == "S2-42"
    )
    lock = {
        **member["candidate_lock"],
        "schema_version": "cara-research-candidate-v3.1",
        "proposal_policy": "spectral-backtrack-v1",
        "study_execution_hash": "study-execution",
        "execution_identity_hash": "trial-execution",
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "weights.bin").write_bytes("测试夹具权重".encode("utf-8"))
    report = _passed_report(
        lock, {"weights.bin": file_digest(root / "weights.bin")}
    )
    report.update(
        schema_version="cara-research-acceptance-v3.1",
        research_claim="recovery_supported",
    )
    return report


class NewResearchArtifactTests(unittest.TestCase):
    def test_old_report_cannot_bind_new_reproduction(self):
        from heretic.ara_research_acceptance import validate_research_binding
        from heretic.ara_research_schema import digest, read_json

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging, formal = root / "staging", root / "formal"
            report = new_passed_report(staging)
            report["schema_version"] = "cara-research-acceptance-v3"
            lock = report["selected_candidate"]
            lock["schema_version"] = "cara-research-candidate-v3"
            for key in (
                "proposal_policy",
                "execution_identity_hash",
                "study_execution_hash",
            ):
                lock.pop(key)
            promote_research_artifact(staging, formal, report)
            reproduction = read_json(formal / "reproduce.json")
            reproduction.update(
                schema="cara-research-reproduce-v3.1",
                execution_identity_hash="unverified-execution",
                study_execution_hash="unverified-study",
            )
            self.assertEqual(reproduction["candidate_lock_hash"], digest(lock))
            with self.assertRaisesRegex(ValueError, "版本"):
                validate_research_binding(report, reproduction)

    def test_new_reproduction_rejects_empty_execution_hashes(self):
        from heretic.ara_research_schema import (
            read_json,
            validate_research_reproduce,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging, formal = root / "staging", root / "formal"
            promote_research_artifact(
                staging, formal, new_passed_report(staging)
            )
            reproduction = read_json(formal / "reproduce.json")
            for key in ("execution_identity_hash", "study_execution_hash"):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    validate_research_reproduce({**reproduction, key: ""})

    def test_old_report_cannot_relabel_new_candidate(self):
        from heretic.ara_research_schema import validate_research_report

        with tempfile.TemporaryDirectory() as directory:
            report = new_passed_report(Path(directory))
            report["schema_version"] = "cara-research-acceptance-v3"
            with self.assertRaisesRegex(ValueError, "版本"):
                validate_research_report(report)

    def test_new_recovery_claim_and_full_artifact_chain(self):
        from heretic.ara_research_acceptance import (
            _research_claim,
            verify_research_artifact_graph,
        )

        self.assertEqual(
            _research_claim(
                "passed",
                "recovery",
                {},
                {"schema_version": "cara-research-protocol-v3.1"},
            ),
            "recovery_supported",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging, destination = root / "staging", root / "formal"
            promote_research_artifact(
                staging, destination, new_passed_report(staging)
            )
            verify_research_artifact_graph(destination)
            (destination / "weights.bin").write_bytes(b"changed")
            with self.assertRaises(ValueError):
                verify_research_artifact_graph(destination)

    def test_pilot_lock_cannot_be_read_as_formal_candidate(self):
        from heretic.ara_research_schema import parse_candidate_lock

        with self.assertRaises(ValueError):
            parse_candidate_lock(
                {
                    "schema_version": "cara-pilot-candidate-lock-v1",
                    "eligibility": "pilot_only",
                }
            )


if __name__ == "__main__":
    unittest.main()
