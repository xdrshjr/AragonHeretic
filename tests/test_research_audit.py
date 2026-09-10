# SPDX-License-Identifier: AGPL-3.0-or-later
"""使用真实磁盘事务验证逐成员消费，避免重跑已消费样本。"""

import tempfile
import unittest
from pathlib import Path

from heretic.ara_research_runner import paired_parameters
from heretic.ara_research_schema import (
    CandidateLock,
    digest,
    read_json,
    write_json,
)
from heretic.research_audit import (
    AuditContext,
    evaluate_audit_member,
    freeze_evaluation_plan,
)


def evaluation_fixture():
    protocol = {
        "protocol_hash": "protocol",
        "seeds": [42, 43, 44],
        "required_level": "statistical_extreme",
        "model_identity": {"revision": "model"},
        "generation_profiles": {"keywords": 100, "semantic": 256},
        "phase_budgets": {
            "audit": {
                "annotation_deadline": "2026-10-01",
                "max_wallclock_seconds": 60,
                "max_gpu_hours": 1,
                "member_budgets": {
                    "B0": {"max_wallclock_seconds": 30},
                    "S2-42": {"max_wallclock_seconds": 30},
                },
                "annotations": {
                    "roles": ["评审甲", "评审乙", "裁决员"],
                    "record_count": 100,
                },
            }
        },
        "roles": {
            "research-audit.good": {"identity": "good"},
            "ability-audit": {"identity": "ability"},
        },
    }
    members = [{"member_id": "B0", "availability": "available", "kind": "base"}]
    for method in ("B1", "B2", "S1", "S2"):
        for seed in (42, 43, 44):
            member = {
                "member_id": f"{method}-{seed}",
                "availability": "unavailable",
                "reason": "未运行",
            }
            if method == "S2" and seed == 42:
                lock = CandidateLock(
                    protocol_hash="protocol",
                    study_hash="study",
                    method_id=method,
                    seed=seed,
                    attempt=0,
                    parameters=paired_parameters(seed, 0),
                    shortlist=[0],
                    selection_rule_hash="selection",
                    final_snapshot_path="factors.pt",
                    final_snapshot_hash="factors",
                    score_evidence_hash="scores",
                    eligibility="qualified",
                )
                member = {
                    "member_id": "S2-42",
                    "availability": "available",
                    "candidate_lock": lock.model_dump(),
                    "candidate_lock_hash": digest(lock.model_dump()),
                }
            members.append(member)
    return freeze_evaluation_plan(members, protocol)


class AuditTests(unittest.TestCase):
    def test_complete_member_recovers_before_exhausted_budget_admission(self):
        from heretic.research_audit import _evaluate_member_roles
        from heretic.research_audit_recovery import initialize_ledgers

        plan = evaluation_fixture()
        member = plan["members"][0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_ledgers(plan, root / "ledgers")
            for role in plan["audit_manifests"]:
                context = AuditContext(
                    root / "ledgers",
                    root / "outputs",
                    role,
                    lambda _: {"status": "passed", "audit_accessed": False},
                    lambda *_: {"is_complete": True},
                )
                evaluate_audit_member(plan, member, context)
            write_json(
                root / "member-budgets" / f"{digest('B0')}.json",
                {"sessions": [{"start": 1, "stop": 31, "devices": 2}]},
            )
            output = _evaluate_member_roles(
                None,
                None,
                plan,
                member,
                (root, root / "ledgers", root / "outputs", {}),
            )
            self.assertEqual(set(output), set(plan["audit_manifests"]))
            self.assertTrue(all("evidence" in row for row in output.values()))

    def test_partial_summary_recovers_complete_and_pending_without_replay(self):
        from heretic.research_audit_recovery import (
            collect_results,
            initialize_ledgers,
        )
        from heretic.ara_research_acceptance import _verify_ledger_bindings

        plan = evaluation_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scope = (root, root / "ledgers", root / "evidence")
            initialize_ledgers(plan, scope[1])
            calls = []
            for role in plan["audit_manifests"]:
                context = AuditContext(
                    scope[1],
                    scope[2],
                    role,
                    lambda _: {"status": "passed", "audit_accessed": False},
                    lambda _, r: calls.append(r) or {"is_complete": True},
                )
                evaluate_audit_member(plan, plan["members"][0], context)
                result = collect_results(plan, scope, "failed")
                _verify_ledger_bindings(plan, result)
            self.assertEqual(len(calls), 2)
            self.assertEqual(
                len(list((root / "audit-history").glob("*.json"))), 2
            )
            self.assertIn("evidence", result["members"]["B0"]["ability-audit"])
            self.assertEqual(
                result["members"]["S2-42"]["ability-audit"]["status"], "not_run"
            )

    def test_failed_audit_writes_offline_summary_and_preserves_pending(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from heretic.research_audit import run_audit_phase
        from heretic.ara_research_acceptance import _verify_ledger_bindings

        plan = evaluation_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "evaluation-plan.json"
            write_json(plan_path, plan)
            args = SimpleNamespace(
                evaluation_plan=str(plan_path),
                run_dir=str(root),
                config="unused",
            )
            settings = SimpleNamespace(
                ara_v3=SimpleNamespace(
                    protocol_manifest=str(root / "protocol.json")
                )
            )
            with (
                patch(
                    "heretic.research_protocol.prepare_protocol",
                    return_value={"protocol_hash": "protocol"},
                ),
                patch(
                    "heretic.research_audit._iterate_audit_members",
                    side_effect=RuntimeError("worker 失败"),
                ),
            ):
                result, code = run_audit_phase(args, settings)
            self.assertEqual(code, 3)
            self.assertEqual(result["research_status"], "failed")
            summary = read_json(root / "audit-results.json")
            self.assertEqual(summary["execution_status"], "failed")
            _verify_ledger_bindings(plan, summary)
            self.assertEqual(
                summary["members"]["B0"]["research-audit.good"]["status"],
                "not_run",
            )

    def test_probe_cache_is_bound_to_config_contents(self):
        from unittest.mock import patch
        from heretic.research_audit import _run_worker

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text("seed=42", encoding="utf-8")
            request = {
                "config": str(config),
                "operation": "probe",
                "member": {"member_id": "B0"},
            }

            def execute(command, **kwargs):
                body = read_json(command[-1])
                write_json(body["output"], {"status": "passed"})

            with patch(
                "heretic.research_audit.subprocess.run", side_effect=execute
            ) as worker:
                _run_worker(dict(request), root, 1)
                _run_worker(dict(request), root, 1)
                self.assertEqual(worker.call_count, 1)
                config.write_text("seed=43", encoding="utf-8")
                _run_worker(dict(request), root, 1)
                self.assertEqual(worker.call_count, 2)

    def test_freeze_rejects_missing_member_and_annotation_budgets(self):
        from heretic.research_audit import _validate_audit_budget

        plan = evaluation_fixture()
        budget = dict(plan["budget"])
        budget["member_budgets"] = {}
        with self.assertRaises(ValueError):
            _validate_audit_budget(budget, plan["members"])
        budget = dict(plan["budget"])
        budget["annotations"] = {}
        with self.assertRaises(ValueError):
            _validate_audit_budget(budget, plan["members"])

    def test_same_plan_can_consume_each_member_once(self):
        plan = evaluation_fixture()
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = AuditContext(
                root / "ledgers",
                root / "outputs",
                "research-audit.good",
                lambda member: {"status": "passed", "audit_accessed": False},
                lambda member, role: (
                    calls.append((member["member_id"], role))
                    or {"is_complete": True, "responses": []}
                ),
            )
            for member in (plan["members"][0], plan["members"][-3]):
                evaluate_audit_member(plan, member, context)
                evaluate_audit_member(plan, member, context)
            self.assertEqual(len(calls), 2)
            for path in (root / "ledgers").glob("*.json"):
                ledger = read_json(path)
                self.assertEqual(len(ledger["events"]), 4)

    def test_failed_reload_does_not_consume_audit(self):
        plan = evaluation_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = AuditContext(
                root / "ledgers",
                root / "outputs",
                "research-audit.good",
                lambda member: {"status": "failed"},
                lambda member, role: self.fail("不得执行审计"),
            )
            with self.assertRaises(ValueError):
                evaluate_audit_member(plan, plan["members"][0], context)
            ledger = read_json(next((root / "ledgers").glob("*.json")))
            self.assertTrue(
                all(
                    row["state"] == "pending"
                    for row in ledger["members"].values()
                )
            )

    def test_consumed_failure_is_never_reexecuted(self):
        plan = evaluation_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []

            def fail(member, role):
                calls.append(role)
                raise RuntimeError("故障发生在消费后")

            context = AuditContext(
                root / "ledgers",
                root / "outputs",
                "research-audit.good",
                lambda member: {"status": "passed", "audit_accessed": False},
                fail,
            )
            with self.assertRaises(RuntimeError):
                evaluate_audit_member(plan, plan["members"][0], context)
            result = evaluate_audit_member(plan, plan["members"][0], context)
            self.assertEqual(result["status"], "inconclusive")
            self.assertEqual(len(calls), 1)

    def test_complete_output_recovers_missing_terminal_write_without_model(
        self,
    ):
        plan = evaluation_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            context = AuditContext(
                root / "ledgers",
                root / "outputs",
                "research-audit.good",
                lambda member: {"status": "passed", "audit_accessed": False},
                lambda member, role: (
                    calls.append(role) or {"is_complete": True}
                ),
            )
            evaluate_audit_member(plan, plan["members"][0], context)
            path = next((root / "ledgers").glob("*.json"))
            ledger = read_json(path)
            complete = next(
                row
                for row in ledger["members"].values()
                if row["state"] == "complete"
            )
            complete["state"] = "consumed"
            write_json(path, ledger)
            result = evaluate_audit_member(plan, plan["members"][0], context)
            self.assertIn("evidence", result)
            self.assertEqual(len(calls), 1)

    def test_manifest_cannot_be_rebound_to_a_new_plan(self):
        plan = evaluation_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = AuditContext(
                root / "ledgers",
                root / "outputs",
                "research-audit.good",
                lambda member: {"status": "passed", "audit_accessed": False},
                lambda member, role: {"is_complete": True},
            )
            evaluate_audit_member(plan, plan["members"][0], context)
            changed = {**plan, "required_level": "recovery"}
            changed.pop("plan_hash")
            changed["plan_hash"] = digest(changed)
            with self.assertRaises(ValueError):
                evaluate_audit_member(changed, changed["members"][0], context)


if __name__ == "__main__":
    unittest.main()
