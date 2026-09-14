"""新执行身份与原生证据放行，不将工程完成解释为效果通过。"""

import copy
import math
import tempfile
import unittest
from pathlib import Path

import torch

from heretic.ara_pilot import (
    PilotGateError,
    bound_record,
    build_pilot_readiness,
    lock_pilot_candidate,
    validate_phase_readiness,
    validate_readiness,
)
from heretic.ara_refinement_capture import tensor_identity
from heretic.ara_refinement_config import RefinementConfig, PARAMETER_RANGES
from heretic.ara_research_runner import paired_parameters
from heretic.ara_research_schema import (
    StageExecution,
    build_execution_identity,
    digest,
    file_digest,
    validate_target_contract,
    write_json,
)


def contract_fixture():
    parameters = paired_parameters(42, 0).model_dump()
    execution = StageExecution(
        campaign_id="test",
        stage="R1",
        stage_execution_id="r1-backtrack-anchor0",
        member_id="S2/spectral-backtrack-v1/42",
        profile="smoke",
        purpose="progress",
        parameters=parameters,
        parameter_hash=digest(parameters),
        initialization_attempt=0,
        operation="trial",
        budget_key="R1",
    ).model_dump(mode="json")
    mappings = {
        f"{role}.{side}": [f"{role}-{side}-{n}" for n in range(count)]
        for role, count in (
            ("fit", 8),
            ("monitor", 8),
            ("development", 100),
            ("mechanism-development", 44),
        )
        for side in ("good", "bad")
    }
    executions = [execution]
    for key, policy in (
        ("r1-reject-anchor0", "reject-v1"),
        ("r1-scale-anchor0", "scale-v1"),
        ("r1-clip-anchor0", "spectral-clip-v1"),
        ("r1-reject-quarter-anchor0", "reject-v1"),
        ("r2-backtrack-anchor0", "spectral-backtrack-v1"),
        ("r2-backtrack-anchor2-resource", "spectral-backtrack-v1"),
    ):
        values = paired_parameters(
            42, 2 if key.endswith("resource") else 0
        ).model_dump()
        if "quarter" in key:
            for name in ("attn_strength", "mlp_strength"):
                values[name] *= 0.25
        executions.append(
            {
                **execution,
                "stage_execution_id": key,
                "member_id": f"S2/{policy}/42",
                "stage": "R2" if key.startswith("r2") else "R1",
                "profile": "full-calibration"
                if key.startswith("r2")
                else "smoke",
                "budget_key": "R2" if key.startswith("r2") else "R1",
                "purpose": (
                    "resource"
                    if key.endswith("resource")
                    else "progress"
                    if key == "r2-backtrack-anchor0"
                    else "comparison"
                ),
                "parameters": values,
                "parameter_hash": digest(values),
            }
        )
    for parent_id, operations in (
        ("r1-backtrack-anchor0", ("reload",)),
        (
            "r2-backtrack-anchor0",
            ("replay-1", "replay-2", "third-apply", "reload"),
        ),
    ):
        parent = next(
            row for row in executions if row["stage_execution_id"] == parent_id
        )
        for operation in operations:
            executions.append(
                {
                    **parent,
                    "stage_execution_id": parent_id + "-" + operation,
                    "operation": operation,
                    "purpose": "replay",
                    "parent_stage_execution_id": parent_id,
                }
            )
    contract = {
        "schema_version": "cara-target-execution-v1",
        "campaign_id": "test",
        "model_identity": {"revision": "test-model"},
        "tokenizer_identity": {"revision": "test-tokenizer"},
        "quantization": "bnb_4bit",
        "dtype": "bfloat16",
        "initialization_sources": {"good": "g", "bad": "b"},
        "source_files": {"test.py": "hash"},
        "package_versions": {"torch": "test"},
        "members": sorted({row["member_id"] for row in executions}),
        "seeds": [42, 43, 44],
        "parameter_ranges": {k: list(v) for k, v in PARAMETER_RANGES.items()},
        "initialization_scheme": "paired-data-v3.1",
        "role_layout": "pilot-preserving-v1",
        "role_mappings": {
            "smoke": mappings,
            "full-calibration": {
                **mappings,
                **{
                    f"{role}.{side}": [
                        f"{role}-{side}-{n}" for n in range(count)
                    ]
                    for role, count in (("fit", 192), ("monitor", 64))
                    for side in ("good", "bad")
                },
            },
        },
        "generation_profiles": {"keywords": 100},
        "scorer_identity": {"version": "test"},
        "hardware": {"devices": 1, "device_names": ["测试 GPU"]},
        "stage_executions": executions,
        "phase_budgets": {
            key: {
                "max_wallclock_seconds": 28800,
                "max_gpu_hours": 8,
                "devices": 1,
                "ledger_path": (
                    Path(tempfile.gettempdir()) / f"ara-fixture-{key}.json"
                ).as_posix(),
            }
            for key in ("R1", "R2")
        },
        "validation_rules": {
            "rank": 128,
            "max_singular_value": 8.0,
            "max_cumulative_ratio": 0.60,
            "relative_change": 1e-6,
            "backtracking_alphas": [1, 0.5, 0.25, 0.125, 0.0625],
        },
    }
    from heretic.ara_refinement_config import freeze_fit_ids

    for profile, mapping in contract["role_mappings"].items():
        mapping["_fit_selected_ids"] = freeze_fit_ids(mapping, profile)
    from heretic.research_protocol import role_execution_identity

    contract["role_identities"] = {
        profile: {
            role: role_execution_identity(bundle)
            for role, bundle in _fixture_roles(mapping, profile).items()
        }
        for profile, mapping in contract["role_mappings"].items()
    }
    return contract


def _fixture_roles(mapping, profile):
    return {
        role: {
            "selected_count": (8 if profile == "smoke" else 96)
            if role.startswith("fit.")
            else len(ids),
            "candidate_count": len(ids),
            "prompts": [{"prompt_id": key} for key in ids],
            "body_hash": digest(ids),
            "source": {"fixture": "shared", "path": f"{role}.json"},
        }
        for role, ids in mapping.items()
        if role != "_fit_selected_ids"
    }


def _fixture_protocol(contract, profile):
    source = {
        key: contract[key]
        for key in (
            "model_identity",
            "tokenizer_identity",
            "quantization",
            "dtype",
            "source_files",
            "package_versions",
            "generation_profiles",
            "initialization_sources",
            "role_layout",
        )
    }
    source.update(
        schema_version="cara-research-protocol-v3.1",
        pilot=True,
        pilot_profile=profile,
        target_execution_contract=contract,
        target_execution_hash=digest(contract),
        judge_identity=contract["scorer_identity"],
        replay_weight_comparison="effective-update-v1",
        roles=_fixture_roles(contract["role_mappings"][profile], profile),
    )
    source["protocol_hash"] = digest(source)
    return source


def pilot_fixture(root):
    contract = contract_fixture()
    target = digest(contract)
    execution = contract["stage_executions"][0]
    source = _fixture_protocol(contract, "smoke")
    config = RefinementConfig(
        protocol_manifest="protocol.json",
        artifact_schema="cara-research-acceptance-v3.1",
        proposal_policy="spectral-backtrack-v1",
        backtracking_alphas=(1, 0.5, 0.25, 0.125, 0.0625),
        stage_execution_id=execution["stage_execution_id"],
    )
    identity = build_execution_identity(
        source, config, 42, (paired_parameters(42, 0), 0)
    )
    snapshot = {"x.A": torch.eye(2), "x.B": torch.eye(2) * 0.1}
    path = root / "factors.pt"
    torch.save(snapshot, path)
    before = {
        "keywords": 0.5,
        "log_odds": 0.0,
        "first_token_kl": 0.0,
        "sequence_kl": 0.0,
        "sample_count": 8,
        "data_identity": "monitor",
    }
    after = {**before, "log_odds": -0.01}
    event = {
        "accepted": True,
        "selected_alpha": 1.0,
        "monitor_before": before,
        "backtracking_attempts": [
            {
                "accepted": True,
                "alpha": 1.0,
                "guards": {"monitor": True},
                "monitor_after": after,
                "statistics": {
                    "x": {
                        "projected_spectral_norm": 0.1,
                        "local_cumulative_ratio": 0.1,
                    }
                },
                "actual_cumulative_ratios": {"x": 0.1},
            }
        ],
    }
    scores = {
        "baseline_keywords": 1.0,
        "keywords": 0.95,
        "first_token_kl": 0.01,
        "sequence_kl": 0.01,
        "log_odds": -0.01,
        "score_identities": {
            side: {
                "prompt_ids": contract["role_mappings"]["smoke"][
                    f"development.{side}"
                ],
                "generation_profile_hash": digest(
                    contract["generation_profiles"]
                ),
                "base_identity": digest(contract["model_identity"]),
            }
            for side in ("good", "bad")
        },
    }
    trial = {
        "schema_version": "cara-research-trial-v3.1",
        "state": "COMPLETE",
        "protocol_hash": source["protocol_hash"],
        "attempt": 0,
        "stage_execution_id": execution["stage_execution_id"],
        "execution_identity": identity,
        "execution_identity_hash": digest(identity),
        "pilot_execution_config_hash": identity["pilot_execution_config_hash"],
        "events": [event],
        "accepted_blocks": 1,
        "changed_modules": ["x"],
        "effective_updates": {
            "x": {"initial_norm": 0, "relative_change": math.sqrt(2) * 0.1}
        },
        "final_snapshot_path": str(path),
        "snapshot_file_hash": file_digest(path),
        "final_snapshot_hash": tensor_identity(snapshot),
        "scores": scores,
        "worker_pid": 10,
    }
    write_json(root / "trial.json", trial)
    evidence = {
        "target_execution_hash": target,
        "stage": "active_update",
        "trial": bound_record(root / "trial.json"),
    }
    lock = lock_pilot_candidate(evidence, execution)
    reload = {
        **trial,
        "operation": "reload",
        "worker_pid": 20,
        "audit_accessed": False,
        "probe_match": True,
    }
    ledger = {
        "target_execution_hash": target,
        "campaign_id": "test",
        "sessions": [{"start": 1, "stop": 2, "devices": 1}],
        "calls": {
            row["stage_execution_id"]: {
                "execution_hash": digest(row),
                "state": "COMPLETE",
            }
            for row in contract["stage_executions"]
            if row["stage"] == "R1"
        },
    }
    for name, value in (
        ("lock", lock),
        ("protocol", source),
        ("reload", reload),
        ("stage_ledger", ledger),
    ):
        write_json(root / f"{name}.json", value)
        evidence[name] = bound_record(root / f"{name}.json")
    return contract, evidence


def resource_fixture(root):
    contract = contract_fixture()
    member = "S2/spectral-backtrack-v1/42"
    for stage in ("R1", "R2"):
        contract["phase_budgets"][stage]["ledger_path"] = (
            root / f"{stage}.json"
        ).as_posix()
    contract["phase_budgets"]["search"] = {
        "ledger_path": (root / "matrix.json").as_posix(),
        "members": {
            member: {
                "max_wallclock_seconds": 28800,
                "max_gpu_hours": 8,
                "devices": 1,
            }
        },
    }
    protocol = _fixture_protocol(contract, "full-calibration")
    write_json(root / "protocol.json", protocol)
    ledger = {
        "target_execution_hash": digest(contract),
        "campaign_id": "test",
        "stage": "R2",
        "sessions": [],
        "calls": {},
    }
    operations = {}
    for row in contract["stage_executions"]:
        if row["stage"] != "R2":
            continue
        trial = _resource_trial_fixture(protocol, row)
        path = root / f"{row['stage_execution_id']}.json"
        write_json(path, trial)
        files = {"trial": bound_record(path)}
        if row["purpose"] == "resource":
            extra = {
                "source_trial": files["trial"],
                "execution_identity": trial["execution_identity"],
                "execution_identity_hash": trial["execution_identity_hash"],
                "measurements": [
                    _cost_record("shortlist"),
                    _cost_record("export"),
                ],
            }
            write_json(root / "resource.json", extra)
            files["resource"] = bound_record(root / "resource.json")
            operations["resource"] = files["resource"]
        operations[row["operation"]] = files["trial"]
        ledger["calls"][row["stage_execution_id"]] = {
            "execution_hash": digest(row),
            "state": "COMPLETE",
            "evidence_hash": digest(trial),
            "evidence_files": files,
            "source_protocol": bound_record(root / "protocol.json"),
            "source_protocol_hash": protocol["protocol_hash"],
            "session_index": len(ledger["sessions"]),
            "started_at": 1,
            "stopped_at": 9,
        }
        ledger["sessions"].append(
            {
                "start": 0,
                "stop": 10,
                "devices": 1,
                "stage_execution_id": row["stage_execution_id"],
            }
        )
    terms = _cost_terms_fixture(operations)
    return contract, {"members": {member: terms}}, ledger


def _cost_record(phase):
    return {"phase": phase, "status": "complete", "wallclock_seconds": 1.0}


def _resource_trial_fixture(protocol, execution):
    from heretic.ara_research_schema import RefinementParameters

    config = RefinementConfig(
        protocol_manifest="protocol.json",
        artifact_schema="cara-research-acceptance-v3.1",
        proposal_policy="spectral-backtrack-v1",
        backtracking_alphas=(1, 0.5, 0.25, 0.125, 0.0625),
        pilot_profile="full-calibration",
        stage_execution_id=(
            execution["parent_stage_execution_id"]
            or execution["stage_execution_id"]
        ),
    )
    identity = build_execution_identity(
        protocol,
        config,
        42,
        (
            RefinementParameters.model_validate(execution["parameters"]),
            execution["initialization_attempt"],
        ),
    )
    operation = execution["operation"]
    return {
        "state": "COMPLETE",
        "protocol_hash": protocol["protocol_hash"],
        "execution_identity": identity,
        "execution_identity_hash": digest(identity),
        "workload_evidence": {
            "target_modules": 2,
            "observed_modules": 2,
            "sweeps": 2,
            "max_backtracks": 5,
        },
        "measurements": {
            "load": _cost_record("load"),
            "reload" if operation == "reload" else "trial": _cost_record(
                "pilot" if operation == "trial" else operation
            ),
        },
        "events": [
            _cost_record(phase)
            for phase in (
                "capture",
                "local_optimization",
                "keywords",
                "prefix_log_odds",
                "sequence_kl",
                "actual_forward",
                "monitor_after",
            )
        ],
    }


def _cost_terms_fixture(operations):
    mapping = {
        "load": [("trial", "load", "load")],
        "trial_bound": [("trial", "pilot", "trial")],
        "shortlist_bound": [("resource", "shortlist", 0)],
        "replay_bound": [
            ("replay-1", "replay-1", "trial"),
            ("replay-2", "replay-2", "trial"),
        ],
        "apply_reload_export_bound": [
            ("third-apply", "third-apply", "trial"),
            ("reload", "reload", "reload"),
            ("resource", "export", 1),
        ],
    }
    return {
        name: {
            "observations": [
                {
                    "source": operations[operation],
                    "phase": phase,
                    "record_path": ["measurements", record],
                }
                for operation, phase, record in items
            ],
            "workload_multiplier": 3 if name == "shortlist_bound" else 1,
            "covered_branches": [phase for _, phase, _ in items],
            "workload": {"fixture": True},
        }
        for name, items in mapping.items()
    }


class PilotTests(unittest.TestCase):
    def test_readiness_rechecks_self_consistent_source_precision_and_scorer(
        self,
    ):
        from heretic.ara_pilot import read_bound, _validate_source_context

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            contract, evidence = pilot_fixture(root)
            original = read_bound(evidence["protocol"])
            trial = read_bound(evidence["trial"])
            for field, value in (
                ("dtype", "float32"),
                ("judge_identity", {"version": "other"}),
            ):
                source = copy.deepcopy(original)
                source[field] = value
                source.pop("protocol_hash")
                source["protocol_hash"] = digest(source)
                trial["protocol_hash"] = source["protocol_hash"]
                write_json(root / "drift-protocol.json", source)
                changed = {
                    **evidence,
                    "protocol": bound_record(root / "drift-protocol.json"),
                }
                with self.subTest(field=field), self.assertRaises(ValueError):
                    _validate_source_context(changed, contract, trial)

    def test_native_evidence_can_pass_and_is_recomputed(self):
        with tempfile.TemporaryDirectory() as folder:
            contract, evidence = pilot_fixture(Path(folder))
            record = build_pilot_readiness(evidence, contract)
            self.assertEqual(record["status"], "passed")
            execution = {
                "required_stage": "active_update",
                "target_execution_contract": contract,
            }
            validate_readiness(record, execution)
            record["accepted_blocks"] = 50
            with self.assertRaises(PilotGateError):
                validate_readiness(record, execution)

    def test_legacy_summary_does_not_open_expansion(self):
        with self.assertRaises(PilotGateError):
            validate_readiness({"status": "passed", "exit_code": 0}, {})

    def test_full_calibration_pilot_requires_previous_proof(self):
        protocol = {
            "schema_version": "cara-research-protocol-v3.1",
            "pilot_profile": "full-calibration",
            "phase_budgets": {},
        }
        with self.assertRaises(PilotGateError):
            validate_phase_readiness(protocol, "pilot")
        protocol["pilot_profile"] = "smoke"
        validate_phase_readiness(protocol, "pilot")

    def test_target_rejects_self_or_readiness_hash(self):
        contract = contract_fixture()
        validate_target_contract(contract)
        contract["readiness"] = "future-output"
        with self.assertRaises(ValueError):
            validate_target_contract(contract)

    def test_parameter_identity_changes_while_study_stays_fixed(self):
        protocol = {"protocol_hash": "p", "pilot": False}
        config = RefinementConfig(protocol_manifest="p")
        first = build_execution_identity(
            protocol, config, 42, (paired_parameters(42, 0), 0)
        )
        second = build_execution_identity(
            protocol, config, 42, (paired_parameters(42, 1), 1)
        )
        self.assertEqual(
            first["study_execution_hash"], second["study_execution_hash"]
        )
        self.assertNotEqual(digest(first), digest(second))

    def test_file_mutation_is_rejected_before_declared_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            contract, evidence = pilot_fixture(root)
            changed = copy.deepcopy(contract)
            changed["model_identity"] = {"revision": "other"}
            with self.assertRaises(PilotGateError):
                build_pilot_readiness(evidence, changed)
            (root / "trial.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(PilotGateError):
                build_pilot_readiness(evidence, contract)


class NativeResourceGateTests(unittest.TestCase):
    def test_complete_cost_cannot_charge_an_unrelated_subphase(self):
        from heretic.research_budget import _validate_measured_branches

        phases = [
            "capture",
            "local_optimization",
            "keywords",
            "prefix_log_odds",
            "sequence_kl",
            "actual_forward",
            "monitor_after",
            "pilot",
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "native.json"
            write_json(
                path,
                {
                    "measurements": {
                        name: {
                            "phase": name,
                            "status": "complete",
                            "wallclock_seconds": 1,
                        }
                        for name in phases
                    }
                },
            )
            row = {
                "observations": [
                    {
                        "source": bound_record(path),
                        "phase": "capture",
                        "record_path": ["measurements", "capture"],
                    }
                ],
                "covered_branches": phases,
            }
            with self.assertRaises(PilotGateError):
                _validate_measured_branches(
                    "trial_bound", row, "S2/spectral-backtrack-v1/42"
                )

    def test_relative_stage_ledger_is_rejected_but_linux_absolute_is_valid(
        self,
    ):
        contract = contract_fixture()
        for stage in ("R1", "R2"):
            contract["phase_budgets"][stage]["ledger_path"] = (
                f"/campaign/{stage}.json"
            )
        validate_target_contract(contract)
        contract["phase_budgets"]["R1"]["ledger_path"] = "R1.json"
        with self.assertRaises(ValueError):
            validate_target_contract(contract)

    def test_missing_expensive_branch_cannot_be_replaced_by_declared_seconds(
        self,
    ):
        from heretic.research_budget import _validate_measured_branches

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            record = {
                "measurements": [
                    {
                        "phase": "capture",
                        "status": "complete",
                        "wallclock_seconds": 1.0,
                    }
                ]
            }
            write_json(root / "cost.json", record)
            row = {
                "observations": [
                    {
                        "source": bound_record(root / "cost.json"),
                        "phase": "pilot",
                        "record_path": ["measurements", "trial"],
                    }
                ],
                "covered_branches": ["capture", "monitor_after"],
            }
            with self.assertRaises(PilotGateError) as caught:
                _validate_measured_branches(
                    "trial_bound", row, "S2/spectral-backtrack-v1/42"
                )
            self.assertEqual(caught.exception.exit_code, 5)

    def test_native_phase_costs_use_frozen_formula_and_required_coverage(self):
        from heretic.research_budget import pilot_cost_prediction

        with tempfile.TemporaryDirectory() as folder:
            contract, resource, ledger = resource_fixture(Path(folder))
            member = "S2/spectral-backtrack-v1/42"
            result = pilot_cost_prediction(resource, contract, ledger)
            self.assertEqual(
                result["members"][member]["predicted_seconds"], 41.25
            )
            resource["members"][member]["shortlist_bound"][
                "workload_multiplier"
            ] = 1
            with self.assertRaises(PilotGateError):
                pilot_cost_prediction(resource, contract, ledger)

    def test_cost_source_requires_registered_complete_r2_call(self):
        from heretic.research_budget import pilot_cost_prediction

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            contract, resource, ledger = resource_fixture(root)
            for mutation in ("missing_call", "failed_call", "unbilled_call"):
                changed = copy.deepcopy(ledger)
                call = changed["calls"]["r2-backtrack-anchor2-resource"]
                if mutation == "missing_call":
                    call["evidence_files"] = {}
                elif mutation == "failed_call":
                    call["state"] = "FAIL"
                else:
                    call["session_index"] = 100
                with (
                    self.subTest(mutation=mutation),
                    self.assertRaises(PilotGateError),
                ):
                    pilot_cost_prediction(resource, contract, changed)

    def test_cost_source_rejects_self_consistent_protocol_drift(self):
        from heretic.ara_pilot import read_bound
        from heretic.research_budget import pilot_cost_prediction

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            contract, resource, ledger = resource_fixture(root)
            for field, value in (
                ("pilot_profile", "smoke"),
                ("dtype", "float32"),
                ("judge_identity", {"version": "other"}),
            ):
                changed = copy.deepcopy(ledger)
                source = read_bound(
                    changed["calls"]["r2-backtrack-anchor2-resource"][
                        "source_protocol"
                    ]
                )
                source[field] = value
                source.pop("protocol_hash")
                source["protocol_hash"] = digest(source)
                write_json(root / "drift.json", source)
                for call in changed["calls"].values():
                    call["source_protocol"] = bound_record(root / "drift.json")
                    call["source_protocol_hash"] = source["protocol_hash"]
                with (
                    self.subTest(field=field),
                    self.assertRaisesRegex(
                        ValueError, "完整 R2 校准|协议.*目标执行契约不匹配"
                    ),
                ):
                    pilot_cost_prediction(resource, contract, changed)

    def test_single_card_budget_cannot_reuse_a_more_generous_contract(self):
        from heretic.ara_pilot import validate_experiment_budget

        member = "S2/spectral-backtrack-v1/42"
        contract = {
            "phase_budgets": {
                "experiment": {
                    "members": {
                        member: {
                            "devices": 1,
                            "max_wallclock_seconds": 28800,
                            "max_gpu_hours": 8,
                        }
                    }
                }
            }
        }
        readiness = {
            "resource_prediction": {
                "members": {member: {"predicted_seconds": 100}}
            }
        }
        validate_experiment_budget(contract, readiness, 42, 8)
        with self.assertRaises(PilotGateError):
            validate_experiment_budget(contract, readiness, 42, 1)


if __name__ == "__main__":
    unittest.main()
