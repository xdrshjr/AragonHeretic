# SPDX-License-Identifier: AGPL-3.0-or-later
"""独立研究搜索、累计成本、候选锁定及重放入口。"""

from __future__ import annotations

import argparse
import math
import random
import subprocess
import time
from pathlib import Path

from .ara_refinement_config import (
    ANCHORS,
    PARAMETER_RANGES,
    load_refinement_settings as _load_settings,
)
from .ara_research_acceptance import (
    development_gate,
    development_semantic_gate,
    finalize_to_file,
)
from .ara_research_schema import (
    STUDY_SCHEMA,
    CandidateLock,
    CandidateLockV31,
    STUDY_SCHEMA_V31,
    study_identity as _study_identity,
    parse_candidate_lock,
    RefinementParameters,
    digest,
    exclusive_lock,
    file_digest,
    read_json,
    write_json,
)
from .ara_pilot import compare_replay
from .research_budget import (
    ResearchBudgetExceeded,
    StudyBudget as StudyBudget,
    run_budgeted_phase,
    campaign_search_status,
    write_cost_checkpoints as _write_checkpoints,
)


class NoResearchCandidate(RuntimeError):
    """没有可用于正式提升的冻结候选。"""


def sample_v3_parameters(trial):
    """方法中立接口的六维 v3 参数采样。"""
    return RefinementParameters(
        **{
            name: trial.suggest_float(
                name, *bounds, log=name not in {"layer_start", "layer_span"}
            )
            for name, bounds in PARAMETER_RANGES.items()
        }
    )


def apply_v3_method(model, artifacts, parameters):
    """通用 apply 接口显式恢复已评分快照，cleanup 仍恢复初态。"""
    import torch
    from .ara_refinement import RefinementArtifacts, apply_refinement_trial
    from .ara_refinement_capture import apply_snapshot
    from .trial_methods import MethodApplicationSummary
    from .config import AbliterationMethod

    if not isinstance(artifacts, RefinementArtifacts):
        raise TypeError("v3 参数必须配合 RefinementArtifacts")
    evidence = apply_refinement_trial(model, artifacts, parameters)
    factors = torch.load(
        evidence["final_snapshot_path"], map_location="cpu", weights_only=True
    )
    apply_snapshot(model.ara_targets, factors, evidence["final_snapshot_hash"])
    return MethodApplicationSummary(AbliterationMethod.ARA, research=evidence)


def validate_research_model(model, report):
    """复现基座必须匹配验收记录中的运行时模型身份。"""
    if report.get("model_fingerprint") != model.model_fingerprint:
        raise ValueError("v3 重载基座模型身份不匹配")


def paired_parameters(seed: int, attempt: int) -> RefinementParameters:
    """首 12 个参数不含方法名，保持方法间逐项配对。"""
    if not 0 <= attempt < 12:
        raise ValueError("TPE 阶段不能使用配对随机参数")
    if attempt < 4:
        return RefinementParameters(
            **dict(zip(PARAMETER_RANGES, ANCHORS[attempt]))
        )
    generator = random.Random(
        int(digest([seed, attempt, "exploration"])[:16], 16)
    )
    values = {}
    for name, (lower, upper) in PARAMETER_RANGES.items():
        if name in {"layer_start", "layer_span"}:
            values[name] = generator.uniform(lower, upper)
        else:
            values[name] = math.exp(
                generator.uniform(math.log(lower), math.log(upper))
            )
    return RefinementParameters(**values)


def _trial_sort(row):
    return (
        row["scores"]["keywords"],
        row["scores"]["first_token_kl"],
        row["attempt"],
    )


def select_research_candidate(study: dict, protocol: dict) -> CandidateLock:
    """仅消费固定 shortlist 的开发证据，永远不读取审计。"""
    trials = study["trials"]
    complete = [row for row in trials if row["state"] == "COMPLETE"]
    if len(trials) != 24 or len(complete) < 23:
        raise NoResearchCandidate("正式搜索健康计数未达标")
    feasible = [row for row in complete if development_gate(row["scores"])]
    shortlist = sorted(feasible, key=_trial_sort)[:3]
    selected = next(
        (
            row
            for row in shortlist
            if development_semantic_gate(row.get("development_semantics", {}))
        ),
        None,
    )
    if selected is None:
        raise NoResearchCandidate("固定 shortlist 没有通过语义/序列门槛的候选")
    return _candidate_lock(study, protocol, selected, shortlist, "qualified")


def _candidate_lock(study, protocol, selected, shortlist, eligibility):
    if (
        file_digest(selected["final_snapshot_path"])
        != selected["snapshot_file_hash"]
    ):
        raise ValueError("候选快照文件已损坏")
    constructor, extra = CandidateLock, {}
    if protocol.get("schema_version") == "cara-research-protocol-v3.1":
        constructor = CandidateLockV31
        extra = {
            "proposal_policy": study["proposal_policy"],
            "study_execution_hash": study["study_execution_hash"],
            "execution_identity_hash": selected["execution_identity_hash"],
        }
        if selected["study_execution_hash"] != study["study_execution_hash"]:
            raise ValueError("候选 trial 不属于当前 study")
    return constructor(
        **extra,
        protocol_hash=protocol["protocol_hash"],
        study_hash=study["study_hash"],
        method_id=study["method_id"],
        seed=study["seed"],
        attempt=selected["attempt"],
        parameters=RefinementParameters.model_validate(selected["parameters"]),
        shortlist=[row["attempt"] for row in shortlist],
        selection_rule_hash=digest(
            [
                "feasible-top-3",
                "keywords-kl-attempt",
                protocol["judge_identity"],
            ]
        ),
        final_snapshot_path=selected["final_snapshot_path"],
        final_snapshot_hash=selected["final_snapshot_hash"],
        score_evidence_hash=digest(selected["scores"]),
        eligibility=eligibility,
        parent_candidate_lock_hash=study.get("parent_candidate_lock_hash"),
    )


def _constraints(trial):
    scores = trial.user_attrs.get("scores")
    if not scores:
        return (1.0, 1.0, 1.0)
    baseline = scores["baseline_keywords"]
    drop = (
        ((baseline - scores["keywords"]) / baseline) if baseline > 0 else -1.0
    )
    return (
        scores["keywords"] - 0.1,
        0.5 - drop,
        scores["first_token_kl"] - 0.15,
    )


def _build_optuna(history, seed):
    import optuna
    from optuna.distributions import FloatDistribution
    from optuna.trial import TrialState, create_trial

    distributions = {
        name: FloatDistribution(
            *bounds, log=name not in {"layer_start", "layer_span"}
        )
        for name, bounds in PARAMETER_RANGES.items()
    }
    sampler = optuna.samplers.TPESampler(
        seed=seed, n_startup_trials=0, constraints_func=_constraints
    )
    study = optuna.create_study(
        directions=["minimize", "minimize"], sampler=sampler
    )
    for row in history:
        state = (
            TrialState.COMPLETE
            if row["state"] == "COMPLETE"
            else TrialState.FAIL
        )
        scores = row.get("scores", {})
        values = (
            [scores["log_odds"], scores["first_token_kl"]] if scores else None
        )
        attrs = {"scores": scores}
        constraint = _constraints(
            type("ConstraintTrial", (), {"user_attrs": attrs})()
        )
        study.add_trial(
            create_trial(
                state=state,
                params=row["parameters"],
                distributions=distributions,
                values=values if state == TrialState.COMPLETE else None,
                user_attrs=attrs,
                system_attrs={"constraints": constraint},
            )
        )
    return study


def _sample_attempt(history, seed, attempt):
    if attempt < 12:
        return paired_parameters(seed, attempt), None
    # 独立 attempt RNG 是冻结协议的一部分，可从完整终态历史精确恢复。
    sampler_seed = int(digest([seed, attempt, "tpe-rng"])[:8], 16)
    study = _build_optuna(history, sampler_seed)
    trial = study.ask()
    values = {
        name: trial.suggest_float(
            name, *bounds, log=name not in {"layer_start", "layer_span"}
        )
        for name, bounds in PARAMETER_RANGES.items()
    }
    return RefinementParameters(**values), {
        "seed": sampler_seed,
        "history_hash": digest(history),
    }


def run_search(context: dict, *, finalize=None) -> dict:
    """独占锁下恢复 attempt 边界；孤立 RUNNING 占用原预算。"""
    path = Path(context["run_dir"]) / "study.json"
    identity = context["identity"]
    with exclusive_lock(path.with_suffix(".lock")):
        study = (
            read_json(path)
            if path.exists()
            else {
                "schema_version": (
                    STUDY_SCHEMA_V31
                    if "study_execution_hash" in identity
                    else STUDY_SCHEMA
                ),
                **identity,
                "trials": [],
                "study_hash": digest(identity),
                "rng_contract": "attempt-seeded-tpe-v3",
            }
        )
        if study["study_hash"] != digest(identity):
            raise ValueError("研究 study 身份不匹配，必须使用新目录")
        if study.get("candidate_lock"):
            return study
        for row in study["trials"]:
            if row["state"] == "RUNNING":
                _mark_interrupted(row)
        write_json(path, study)
        budget = context["budget"]
        from .research_budget import record_campaign_study

        record_campaign_study(context, path)
        try:
            for attempt in range(len(study["trials"]), 24):
                budget.check()
                _run_attempt(context, study, path, attempt)
        finally:
            if study["schema_version"] == STUDY_SCHEMA_V31:
                _write_checkpoints(
                    study, budget.elapsed() * budget.devices / 3600
                )
                write_json(path, study)
        (finalize or _select_and_lock)(context, study, path)
        return study


def _run_attempt(context, study, path, attempt):
    parameters, rng = _sample_attempt(study["trials"], study["seed"], attempt)
    row = {
        "attempt": attempt,
        "parameters": parameters.model_dump(),
        "state": "RUNNING",
        "rng_state": rng,
        "started_at": time.time(),
    }
    study["trials"].append(row)
    write_json(path, study)
    try:
        evidence = context["apply"](parameters, attempt, "search")
        if evidence["state"] != "COMPLETE":
            raise RuntimeError("trial 回调没有返回完整终态")
        row.update(evidence)
    except Exception as error:
        from .ara_refinement_capture import SnapshotRestoreError
        from .research_budget import _blocking_trial

        row.update(state="FAIL", failure_category=type(error).__name__)
        row["failure_reason"] = str(error)
        if isinstance(
            error, (ResearchBudgetExceeded, SnapshotRestoreError)
        ) or (
            study.get("schema_version") == STUDY_SCHEMA_V31
            and _blocking_trial(row)
        ):
            write_json(path, study)
            raise
    devices = getattr(context["budget"], "devices", 2)
    row["completed_gpu_hours"] = context["budget"].elapsed() * devices / 3600
    _write_checkpoints(study, row["completed_gpu_hours"])
    write_json(path, study)


def _select_and_lock(context, study, path):
    complete = [row for row in study["trials"] if row["state"] == "COMPLETE"]
    feasible = sorted(
        [row for row in complete if development_gate(row["scores"])],
        key=_trial_sort,
    )[:3]
    study["frozen_shortlist"] = [row["attempt"] for row in feasible]
    write_json(path, study)
    if len(complete) >= 23:
        for row in feasible:
            if "development_semantics" in row:
                continue
            context["budget"].check()
            row["development_semantics"] = context["semantic"](row)
            write_json(path, study)
    try:
        lock = select_research_candidate(study, context["protocol"])
        study["effect_status"] = "passed"
    except NoResearchCandidate:
        study["effect_status"] = "failed"
        if not complete:
            study["availability"] = "unavailable"
            write_json(path, study)
            return
        lock = _candidate_lock(
            study,
            context["protocol"],
            min(complete, key=_trial_sort),
            feasible,
            "comparison_only",
        )
    study["candidate_lock"] = lock.model_dump()
    write_json(
        path.parent / "candidate-lock.json", lock.model_dump(), immutable=True
    )
    write_json(path, study)


def replay_locked_candidate(context, study):
    """两次完整重放加第三次 apply 均绑定原搜索状态。"""
    lock = parse_candidate_lock(study["candidate_lock"])
    reference = study["trials"][lock.attempt]
    replays = []
    root = Path(context["run_dir"])
    failure = root / "replay-failure.json"
    if failure.exists():
        raise ValueError("锁定候选曾重放失败，禁止重新搜索或覆盖失败证据")
    for phase in ("replay-1", "replay-2", "third-apply"):
        context["budget"].check()
        evidence_path = root / phase / str(lock.attempt) / "trial.json"
        try:
            actual = (
                read_json(evidence_path)
                if evidence_path.exists()
                else context["apply"](lock.parameters, lock.attempt, phase)
            )
            compare_replay(reference, actual)
        except Exception as error:
            write_json(
                failure, {"phase": phase, "reason": str(error)}, immutable=True
            )
            raise
        replays.append(actual)
    return replays


def _runtime_context(settings, protocol, root, budget):
    return _ResearchSession(settings, protocol, root, budget).context()


def _role_prompts(protocol, protocol_root, role):
    from .research_protocol import load_role_body
    from .utils import Prompt

    return {
        side: [
            Prompt(row["system"], row["text"])
            for row in load_role_body(protocol, protocol_root, f"{role}.{side}")
        ]
        for side in ("good", "bad")
    }


class _ResearchSession:
    def __init__(self, settings, protocol, root, budget, *, snapshots=32):
        import torch
        import numpy as np
        from .model import Model
        from .research_evaluation import RoleEvaluator
        from .ara_refinement_capture import (
            preflight_snapshot_storage,
            remaining_snapshot_count,
        )
        from .research_protocol import fit_selection

        started = time.monotonic()
        torch.manual_seed(settings.seed)
        np.random.seed(settings.seed)
        random.seed(settings.seed)
        self.settings, self.protocol = settings, protocol
        self.root, self.budget = root, budget
        self.model = Model(settings)
        self.model.model.eval()
        if protocol.get("schema_version") == "cara-research-protocol-v3.1":
            planned = 1 if protocol.get("pilot") else snapshots
            snapshots = remaining_snapshot_count(root, planned)
        preflight_snapshot_storage(self.model, root, snapshots=snapshots)
        protocol_root = Path(settings.ara_v3.protocol_manifest).parent
        self.fit, self.fit_ids = fit_selection(
            settings, protocol, protocol_root
        )
        self.monitor = RoleEvaluator(
            self.model,
            protocol,
            "monitor",
            _role_prompts(protocol, protocol_root, "monitor"),
        )
        role = (
            "mechanism-development"
            if settings.ara_v3.method_id in {"A1", "A2", "F1"}
            else "development"
        )
        self.development = RoleEvaluator(
            self.model,
            protocol,
            role,
            _role_prompts(protocol, protocol_root, role),
        )
        self.load_seconds = time.monotonic() - started

    def check_budget(self):
        from .ara_refinement_capture import research_resource_status

        self.budget.check()
        return research_resource_status(self.model)

    def apply(self, parameters, attempt, phase):
        from .ara_refinement import RefinementArtifacts, apply_refinement_trial

        config = self.settings.ara_v3
        if getattr(self, "pilot_parent_id", None):
            config = config.model_copy(
                update={
                    "stage_execution_id": self.pilot_parent_id,
                }
            )
        artifacts = RefinementArtifacts(
            self.protocol,
            config,
            self.fit,
            self.fit_ids,
            self.monitor,
            self.development,
            self.root / phase / str(attempt),
            self.settings.seed,
            attempt,
            self.check_budget,
        )
        artifacts.load_seconds = self.load_seconds
        artifacts.invocation_phase = phase
        return apply_refinement_trial(self.model, artifacts, parameters)

    def semantic(self, row):
        import torch
        from .ara_refinement_capture import apply_snapshot, snapshot_factors
        from .research_evaluation import judge_development

        original = snapshot_factors(self.model.ara_targets)
        try:
            snapshot = torch.load(
                row["final_snapshot_path"],
                map_location="cpu",
                weights_only=True,
            )
            apply_snapshot(
                self.model.ara_targets, snapshot, row["final_snapshot_hash"]
            )
            return judge_development(
                self.development,
                self.model,
                self.protocol["judge_identity"]["development"],
            )
        finally:
            apply_snapshot(self.model.ara_targets, original)

    def context(self):
        return {
            "run_dir": self.root,
            "identity": _study_identity(self.settings, self.protocol),
            "apply": self.apply,
            "semantic": self.semantic,
            "protocol": self.protocol,
            "budget": self.budget,
            "model": self.model,
            "mechanism_evaluate": self.development,
            "check_budget": self.check_budget,
        }


def _mark_interrupted(row):
    row.update(
        state="FAIL",
        failure_category="orphaned_worker",
        interrupted=True,
        recovered_at=time.time(),
    )


def _recover_search_attempts(root, identity):
    """调用方持有 worker 锁；预算耗尽也必须先收尾孤立 attempt。"""
    path = Path(root) / "study.json"
    if not path.exists():
        return
    with exclusive_lock(path.with_suffix(".lock")):
        study = read_json(path)
        if study["study_hash"] != digest(identity):
            raise ValueError("研究 study 身份不匹配，必须使用新目录")
        for row in study["trials"]:
            if row["state"] == "RUNNING":
                _mark_interrupted(row)
        write_json(path, study)


def run_from_settings(settings, phase="preflight", run_dir=None):
    """在模型分配前完成协议检查，并从独立 v3 入口执行。"""
    from .research_protocol import prepare_protocol

    if settings.reproduce:
        from .ara_research_acceptance import reproduce_research_candidate

        return reproduce_research_candidate(settings)
    protocol = prepare_protocol(settings)
    if protocol.get("schema_version") == "cara-research-protocol-v3.1":
        from .ara_pilot import validate_phase_readiness

        target_phase = "pilot" if protocol.get("pilot") else "search"
        validate_phase_readiness(protocol, target_phase)
    root = Path(run_dir or settings.study_checkpoint_dir)
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / "protocol.json", protocol, immutable=True)
    if phase == "preflight":
        return {
            "phase": phase,
            "research_status": "not_run",
            "protocol_hash": protocol["protocol_hash"],
        }, 0
    if phase not in {"pilot", "search"}:
        raise ValueError("模型入口仅支持 preflight/pilot/search")
    if bool(protocol.get("pilot")) != (phase == "pilot"):
        raise ValueError("pilot 与正式搜索协议不可混用")
    from .ara_refinement_config import research_phase_limit

    limit = research_phase_limit(settings, protocol, phase)
    with exclusive_lock(root / "worker.lock"):
        _recover_search_attempts(root, _study_identity(settings, protocol))
        return run_budgeted_phase(settings, protocol, root, phase, limit)


def _execute_model_phase(context, settings, phase):
    if phase == "search" and settings.ara_v3.method_id in {"A1", "A2", "F1"}:
        from .ara_refinement import run_paired_ablation

        return run_paired_ablation(context, settings)
    if phase == "pilot":
        if context["protocol"].get("schema_version", "").endswith("-v3.1"):
            from .ara_pilot import run_registered_pilot

            return run_registered_pilot(context, settings)
        evidence = context["apply"](
            paired_parameters(settings.seed, 0), 0, "pilot"
        )
        return {
            "phase": phase,
            "research_status": "not_run",
            "pilot": evidence,
        }, 0
    return _finish_search(context, Path(context["run_dir"]))


def _finish_search(context, root):
    from .ara_research_acceptance import stage_research_candidate

    study = run_search(context)
    if study.get("candidate_lock"):
        study["replays"] = replay_locked_candidate(context, study)
        _write_checkpoints(
            study,
            context["budget"].elapsed() * context["budget"].devices / 3600,
        )
        write_json(root / "study.json", study)
        if not (root / "member.json").exists():
            stage_research_candidate(context, study)
    campaign_search_status(context["protocol"], context["identity"], study)
    status = study.get("effect_status", "failed")
    return {
        "phase": "search",
        "research_status": "inconclusive" if status == "passed" else "failed",
    }, 0 if status == "passed" else 4


def main():
    """研究 wrapper 的结构化阶段入口，统一传播固定退出码。"""
    parser = argparse.ArgumentParser(description="ARA v3 研究阶段执行")
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument(
        "--phase",
        choices=["preflight", "pilot", "search", "freeze", "audit", "finalize"],
        required=True,
    )
    parser.add_argument("--members")
    parser.add_argument("--evaluation-plan")
    parser.add_argument("--labels")
    args = parser.parse_args()
    root = Path(args.run_dir)
    root.mkdir(parents=True, exist_ok=True)
    try:
        result, code = _dispatch_phase(args)
    except (ValueError, KeyError, TypeError) as error:
        result, code = (
            {
                "phase": args.phase,
                "research_status": "failed",
                "reason": str(error),
            },
            2,
        )
        from .ara_pilot import PilotGateError

        if isinstance(error, PilotGateError):
            code = error.exit_code
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        result, code = _runtime_failure_result(args.phase, error), 3
    result, code = _write_phase_result(root, args.phase, (result, code))
    print(result, flush=True)
    raise SystemExit(code)


def _write_phase_result(root, phase, outcome):
    result, code = outcome
    if phase == "finalize" and code in {2, 3}:
        _write_phase_error(root, phase, result)
        return result, code
    path = root / f"{phase}-result.json"
    try:
        write_json(path, result, immutable=phase == "finalize")
    except ValueError:
        if phase != "finalize" or not path.exists():
            raise
        if code == 0:
            result, code = (
                {
                    "phase": phase,
                    "research_status": "failed",
                    "reason": "已有冻结 finalize 结果，禁止覆盖",
                },
                2,
            )
        _write_phase_error(root, phase, result)
    return result, code


def _write_phase_error(root, phase, result):
    write_json(
        root / "phase-errors" / f"{phase}-{digest(result)}.json",
        result,
        immutable=True,
    )


def _runtime_failure_result(phase, error):
    result = {
        "phase": phase,
        "research_status": "failed",
        "reason": str(error),
        "failure_category": type(error).__name__,
    }
    if isinstance(error, subprocess.CalledProcessError):
        result["subprocess_returncode"] = error.returncode
    if isinstance(error, subprocess.TimeoutExpired):
        result["timeout_seconds"] = error.timeout
    return result


def _dispatch_phase(args):
    from .research_audit import freeze_evaluation_plan

    if args.phase == "finalize":
        if not args.evaluation_plan or not args.labels:
            raise ValueError("finalize 必须指定冻结集合及标签")
        plan = read_json(args.evaluation_plan)
        if (Path(args.run_dir) / "audit-results.json").exists():
            from .ara_research_acceptance import finalize_evaluation_plan

            return finalize_evaluation_plan(
                plan, Path(args.run_dir), read_json(args.labels)
            )
        inputs = read_json(Path(args.run_dir) / "finalize-inputs.json")
        if inputs["artifacts"]["audit_plan_hash"] != plan["plan_hash"]:
            raise ValueError("finalize 输入不属于冻结集合")
        inputs["labels"] = read_json(args.labels)
        return finalize_to_file(inputs, Path(args.run_dir) / "acceptance.json")
    settings = _load_settings(args.config)
    if args.phase == "freeze":
        if not args.members:
            raise ValueError("freeze 必须指定完整成员清单")
        protocol = read_json(settings.ara_v3.protocol_manifest)
        plan = freeze_evaluation_plan(
            read_json(args.members)["members"], protocol
        )
        write_json(
            Path(args.run_dir) / "evaluation-plan.json", plan, immutable=True
        )
        return {
            "phase": "freeze",
            "research_status": "not_run",
            "plan_hash": plan["plan_hash"],
        }, 0
    if args.phase == "audit":
        from .research_audit import run_audit_phase

        return run_audit_phase(args, settings)
    return run_from_settings(settings, args.phase, args.run_dir)


if __name__ == "__main__":
    main()
