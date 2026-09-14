# SPDX-License-Identifier: AGPL-3.0-or-later
"""从原生、可追溯证据派生试点放行；不接受人工成功摘要。"""

from __future__ import annotations

import math
from pathlib import Path

from .ara_research_schema import (
    StageExecution,
    digest,
    file_digest,
    read_json,
    validate_target_contract,
    write_json,
)

RULE = {
    "version": "pilot-gates-v1",
    "change": 1e-6,
    "keywords_count_drop": 5,
    "kl": 0.15,
}


class PilotGateError(ValueError):
    """阶段门控的稳定退出码。"""

    def __init__(self, reason, exit_code=2):
        super().__init__(reason)
        self.exit_code = exit_code


def bound_record(path):
    """构造本地证据引用，不改变被引用文件。"""
    path = Path(path).resolve()
    return {"path": path.as_posix(), "sha256": file_digest(path)}


def read_bound(reference):
    """先验证原文件字节，再读取证据。"""
    if set(reference) != {"path", "sha256"}:
        raise PilotGateError("证据引用字段不完整")
    if file_digest(reference["path"]) != reference["sha256"]:
        raise PilotGateError("来源证据文件摘要不匹配")
    return read_json(reference["path"])


def stage_execution(protocol, execution_id):
    """从已冻结目标契约查找一次调用，禁止临时加试。"""
    contract = protocol["target_execution_contract"]
    if validate_target_contract(contract) != protocol["target_execution_hash"]:
        raise PilotGateError("阶段目标执行契约已变化")
    matches = [
        row
        for row in contract["stage_executions"]
        if row["stage_execution_id"] == execution_id
    ]
    if len(matches) != 1:
        raise PilotGateError("阶段执行 ID 未注册或重复")
    return StageExecution.model_validate(matches[0]).model_dump(mode="json")


def _verify_snapshot(trial):
    import torch
    from .ara_refinement_capture import tensor_identity

    if file_digest(trial["final_snapshot_path"]) != trial["snapshot_file_hash"]:
        raise PilotGateError("pilot 快照文件损坏")
    snapshot = torch.load(
        trial["final_snapshot_path"], map_location="cpu", weights_only=True
    )
    if tensor_identity(snapshot) != trial["final_snapshot_hash"]:
        raise PilotGateError("pilot 快照身份与评分权重不同")
    return snapshot


def lock_pilot_candidate(evidence, execution):
    """锁定预注册 anchor0 原始 trial；永久只用于 pilot。"""
    trial = read_bound(evidence["trial"])
    if execution["purpose"] != "progress" or execution["operation"] != "trial":
        raise PilotGateError("资源探针或对照不能替代进展候选")
    if trial["stage_execution_id"] != execution["stage_execution_id"]:
        raise PilotGateError("pilot trial 与预注册执行 ID 不一致")
    if trial["execution_identity"]["parameters"] != execution["parameters"]:
        raise PilotGateError("pilot 固定参数身份不一致")
    if trial["attempt"] != execution["initialization_attempt"]:
        raise PilotGateError("pilot 初始化 attempt 不一致")
    method, policy, _ = execution["member_id"].split("/")
    if (
        trial["execution_identity"]["method_id"] != method
        or trial["execution_identity"]["proposal_policy"] != policy
    ):
        raise PilotGateError("pilot 原始执行方法或策略不匹配")
    _verify_snapshot(trial)
    return {
        "schema_version": "cara-pilot-candidate-lock-v1",
        "eligibility": "pilot_only",
        "target_execution_hash": evidence["target_execution_hash"],
        "stage_execution_id": execution["stage_execution_id"],
        "source_protocol_hash": trial["protocol_hash"],
        "pilot_execution_config_hash": trial["pilot_execution_config_hash"],
        "execution_identity_hash": trial["execution_identity_hash"],
        "parameters": execution["parameters"],
        "initialization_attempt": trial["attempt"],
        "original_trial": evidence["trial"],
        "score_evidence_hash": digest(trial["scores"]),
        "snapshot": bound_record(trial["final_snapshot_path"]),
        "effective_weight_identity": trial["final_snapshot_hash"],
        "gate_rule_hash": digest(RULE),
    }


def replay_pilot_candidate(context, lock):
    """只执行当前已登记的一次重放；调用方持有共享预算锁。"""
    from .ara_research_schema import RefinementParameters

    if lock["eligibility"] != "pilot_only":
        raise PilotGateError("试点重放只能读取试点锁")
    execution = stage_execution(
        context["protocol"], context["stage_execution_id"]
    )
    if execution["parent_stage_execution_id"] != lock["stage_execution_id"]:
        raise PilotGateError("重放父项与试点锁不一致")
    if execution["operation"] not in {"replay-1", "replay-2", "third-apply"}:
        raise PilotGateError("该调用不是预注册完整重放")
    call = (
        context["budget"].ledger["calls"].get(execution["stage_execution_id"])
    )
    if call is not context["budget"].stage_call or call["state"] != "RUNNING":
        raise PilotGateError("重放尚未登记到共享预算或已终止")
    parameters = RefinementParameters.model_validate(lock["parameters"])
    return _registered_replay(context, execution, parameters)


def _active_evidence(trial):
    if trial.get("schema_version") != "cara-research-trial-v3.1":
        raise PilotGateError("旧 pilot 摘要不能产生新版放行")
    if digest(trial["execution_identity"]) != trial["execution_identity_hash"]:
        raise PilotGateError("trial 执行身份摘要不匹配")
    accepted = [row for row in trial["events"] if row["accepted"]]
    changes = _changed_snapshot_modules(trial)
    if len(accepted) != trial["accepted_blocks"]:
        raise PilotGateError("接受数与原生事件不一致")
    if sorted(changes) != sorted(trial["changed_modules"]):
        raise PilotGateError("有效变化与模块清单不一致")
    guards = []
    for event in accepted:
        selected = [
            row for row in event["backtracking_attempts"] if row["accepted"]
        ]
        if len(selected) != 1:
            raise PilotGateError("接受事件没有唯一回溯提案")
        _verify_accepted_attempt(event, selected[0])
        guards.extend(selected[0]["guards"].values())
    return bool(
        accepted and changes and guards and all(x is True for x in guards)
    )


def _changed_snapshot_modules(trial):
    from .ara_proposal import effective_norm

    snapshot = _verify_snapshot(trial)
    changes = []
    for key in sorted(name for name in snapshot if name.endswith(".A")):
        norm = effective_norm((snapshot[key], snapshot[key[:-1] + "B"]))
        if norm > RULE["change"]:
            changes.append(key[:-2])
        summary = trial["effective_updates"][key[:-2]]
        if summary["initial_norm"] != 0 or not math.isclose(
            norm, summary["relative_change"], rel_tol=1e-6, abs_tol=1e-7
        ):
            raise PilotGateError("有效更新摘要与实际因子不一致")
    return changes


def _verify_accepted_attempt(event, attempt):
    from .ara_refinement import monitor_accepts

    if event["selected_alpha"] != attempt["alpha"]:
        raise PilotGateError("接受步长与尝试记录不一致")
    for row in attempt["statistics"].values():
        if (
            row["projected_spectral_norm"] > 8
            or row["local_cumulative_ratio"] > 0.6
        ):
            raise PilotGateError("接受候选的部署约束失败")
    if any(
        value > 0.6 for value in attempt["actual_cumulative_ratios"].values()
    ):
        raise PilotGateError("接受候选的真实前向约束失败")
    if not monitor_accepts(event["monitor_before"], attempt["monitor_after"]):
        raise PilotGateError("接受事件实际 monitor 门槛未通过")


def _reload_matches(reference, reload):
    from .ara_proposal import compare_effective_factors

    if reload.get("operation") != "reload" or not reload.get("worker_pid"):
        raise PilotGateError("缺少独立进程重载证据", 5)
    if (
        reload.get("execution_identity_hash")
        != reference["execution_identity_hash"]
    ):
        raise PilotGateError("重载执行身份不匹配")
    if reload.get("worker_pid") == reference.get("worker_pid"):
        raise PilotGateError("重载与原始试点不是独立进程")
    if reload.get("audit_accessed") is not False:
        raise PilotGateError("试点重载不能读取审计")
    if reload.get("probe_match") is not True:
        return False
    compare_effective_factors(
        _verify_snapshot(reference), _verify_snapshot(reload)
    )
    _compare_scores(reference["scores"], reload["scores"])
    return True


def build_pilot_readiness(evidence, contract):
    """重算原始 trial、锁、重放、重载和资源条件。"""
    from .research_budget import pilot_cost_prediction

    trial, source, execution = _load_readiness_sources(evidence, contract)
    target_hash = digest(contract)
    active = _active_evidence(trial)
    reload_ok = _reload_matches(trial, read_bound(evidence["reload"]))
    stage = evidence["stage"]
    scores = trial["scores"]
    delta = scores["baseline_keywords"] - scores["keywords"]
    passed = active and reload_ok
    prediction = None
    if stage != "active_update":
        _verify_replays(trial, evidence)
        passed = (
            passed
            and delta >= 0.05 - 1e-12
            and all(
                0 <= scores[name] <= 0.15
                for name in ("first_token_kl", "sequence_kl")
            )
        )
    ledger = read_bound(evidence["stage_ledger"])
    if stage == "search_readiness":
        prediction = pilot_cost_prediction(
            read_bound(evidence["resource"]), contract, ledger
        )
    if ledger["target_execution_hash"] != target_hash:
        raise PilotGateError("阶段共享账本目标不匹配")
    _verify_stage_ledger(ledger, contract, execution)
    return {
        "schema_version": "cara-pilot-readiness-v1",
        "stage": stage,
        "target_execution_hash": target_hash,
        "status": "passed" if passed else "failed",
        "accepted_blocks": trial["accepted_blocks"],
        "changed_modules": trial["changed_modules"],
        "keywords_delta": delta,
        "source_protocol_hash": source["protocol_hash"],
        "role_counts": {
            role: row["selected_count"] for role, row in source["roles"].items()
        },
        "resource_prediction": prediction,
        "source_evidence": evidence,
        "reasons": [] if passed else ["更新、重载或开发改善门槛未通过"],
    }


def _load_readiness_sources(evidence, contract):
    if evidence["stage"] not in {
        "active_update",
        "development_progress",
        "search_readiness",
    }:
        raise PilotGateError("放行阶段未知")
    target_hash = validate_target_contract(contract)
    if evidence["target_execution_hash"] != target_hash:
        raise PilotGateError("放行证据不属于目标契约")
    trial, lock = read_bound(evidence["trial"]), read_bound(evidence["lock"])
    if lock["target_execution_hash"] != target_hash:
        raise PilotGateError("试点锁目标不匹配")
    if lock["original_trial"] != evidence["trial"]:
        raise PilotGateError("放行证据不是锁定原始 trial")
    source, execution = _validate_source_context(evidence, contract, trial)
    if lock != lock_pilot_candidate(evidence, execution):
        raise PilotGateError("试点锁与原始执行证据不一致")
    _verify_snapshot(trial)
    return trial, source, execution


def _validate_source_context(evidence, contract, trial):
    from .research_protocol import _validate_new_protocol

    source = read_bound(evidence["protocol"])
    _validate_new_protocol(source)
    if source.get("target_execution_hash") != digest(contract):
        raise PilotGateError("来源协议绑定了其他目标契约")
    payload = {
        key: value for key, value in source.items() if key != "protocol_hash"
    }
    if digest(payload) != trial["protocol_hash"]:
        raise PilotGateError("来源试点协议与 trial 不一致")
    execution = stage_execution(source, trial["stage_execution_id"])
    expected_id = (
        "r1-backtrack-anchor0"
        if evidence["stage"] == "active_update"
        else "r2-backtrack-anchor0"
    )
    if execution["stage_execution_id"] != expected_id:
        raise PilotGateError("来源不是预注册 anchor0 进展项")
    profile = (
        "smoke" if evidence["stage"] == "active_update" else "full-calibration"
    )
    if source["pilot_profile"] != profile or not source.get("pilot"):
        raise PilotGateError("来源试点角色 profile 不匹配")
    _verify_source_roles(source, contract["role_mappings"][profile])
    for side in ("good", "bad"):
        role = f"development.{side}"
        expected = contract["role_mappings"][profile][role]
        actual = trial["scores"]["score_identities"][side]
        if len(expected) != 100 or actual["prompt_ids"] != expected:
            raise PilotGateError("开发评分没有使用冻结的 100 条问题")
        if actual["generation_profile_hash"] != digest(
            contract["generation_profiles"]
        ):
            raise PilotGateError("开发生成身份不匹配")
        if actual["base_identity"] != digest(contract["model_identity"]):
            raise PilotGateError("开发基座身份不匹配")
    return source, execution


def _verify_source_roles(source, mapping):
    for role, ids in mapping.items():
        if role == "_fit_selected_ids":
            continue
        bundle = source["roles"][role]
        expected = 8 if source["pilot_profile"] == "smoke" else 96
        if not role.startswith("fit."):
            expected = len(ids)
        if (
            bundle["selected_count"] != expected
            or bundle["candidate_count"] != len(ids)
            or [row["prompt_id"] for row in bundle["prompts"]] != ids
        ):
            raise PilotGateError(f"来源角色没有使用冻结布局和计数：{role}")


def _verify_stage_call(row, state):
    from .research_budget import _blocking_trial

    if state["execution_hash"] != digest(
        StageExecution.model_validate(row).model_dump(mode="json")
    ):
        raise PilotGateError("阶段调用注册身份不一致")
    if state["state"] not in {"COMPLETE", "FAIL"}:
        raise PilotGateError("阶段仍有未收尾调用", 5)
    if _blocking_trial(state):
        raise PilotGateError("阶段存在资源或身份故障，禁止继续放行", 3)
    if row["purpose"] != "comparison" and state["state"] != "COMPLETE":
        raise PilotGateError("阶段必需进展、资源或重放调用未成功", 5)


def _verify_stage_ledger(ledger, contract, execution):
    expected = [
        row
        for row in contract["stage_executions"]
        if row["stage"] == execution["stage"]
    ]
    if ledger["campaign_id"] != execution["campaign_id"]:
        raise PilotGateError("共享阶段账本 campaign 不匹配")
    if set(ledger["calls"]) != {row["stage_execution_id"] for row in expected}:
        raise PilotGateError("阶段调用清单不完整或存在额外加试", 5)
    for row in expected:
        _verify_stage_call(row, ledger["calls"][row["stage_execution_id"]])
    budget = contract["phase_budgets"][execution["budget_key"]]
    sessions = ledger["sessions"]
    if not sessions or any(
        row["devices"] != budget["devices"]
        or row.get("stop", row["start"]) < row["start"]
        for row in sessions
        if row.get("stop") is not None
    ):
        raise PilotGateError("共享阶段计费设备或区间非法")
    if any(row.get("stop") is None for row in sessions):
        raise PilotGateError("共享阶段预算尚未结清", 5)
    wall = sum(row["stop"] - row["start"] for row in sessions)
    gpu = sum(
        (row["stop"] - row["start"]) * row["devices"] / 3600 for row in sessions
    )
    if wall > budget["max_wallclock_seconds"] or gpu > budget["max_gpu_hours"]:
        raise PilotGateError("共享阶段累计预算超限", 3)


def _verify_replays(trial, evidence):
    from .ara_research_runner import compare_replay

    replays = [read_bound(row) for row in evidence.get("replays", [])]
    if [row["invocation_id"] for row in replays] != [
        "replay-1",
        "replay-2",
        "third-apply",
    ]:
        raise PilotGateError("缺少两次重放及第三次 apply", 5)
    for row in replays:
        compare_replay(trial, row)


def validate_readiness(record, execution):
    """双入口模型加载前重算证据；时间经过不是放行证明。"""
    if record.get("schema_version") != "cara-pilot-readiness-v1":
        raise PilotGateError("旧摘要没有新版 readiness")
    if record["stage"] != execution["required_stage"]:
        raise PilotGateError("readiness 阶段不匹配")
    derived = build_pilot_readiness(
        record["source_evidence"], execution["target_execution_contract"]
    )
    if derived != record:
        raise PilotGateError("放行记录不符合来源证据")
    if derived["status"] != "passed":
        raise PilotGateError("有效更新或开发进展未达标", 4)


def validate_phase_readiness(protocol, phase):
    """R1 无前证；R2 需要有效更新，扩大运行需要完整资源证明。"""
    if protocol["schema_version"] == "cara-research-protocol-v3":
        return
    if phase == "pilot" and protocol["pilot_profile"] == "smoke":
        return
    required = "active_update" if phase == "pilot" else "search_readiness"
    reference = (
        protocol["phase_budgets"].get(phase, {}).get("readiness_evidence")
    )
    if not reference:
        raise PilotGateError("缺少冻结的阶段放行证据")
    validate_readiness(
        read_bound(reference),
        {
            "required_stage": required,
            "target_execution_contract": protocol["target_execution_contract"],
        },
    )


def validate_experiment_budget(contract, readiness, seed, hours):
    """单卡入口必须满足自身预注册预算，不能复用更宽松的目标上限。"""
    member = f"S2/spectral-backtrack-v1/{seed}"
    budget = (
        contract["phase_budgets"]
        .get("experiment", {})
        .get("members", {})
        .get(member)
    )
    if not budget or budget != {
        "devices": 1,
        "max_wallclock_seconds": hours * 3600,
        "max_gpu_hours": hours,
    }:
        raise PilotGateError("单卡实验 seed/实际小时预算未在目标契约注册")
    prediction = readiness["resource_prediction"]["members"][member][
        "predicted_seconds"
    ]
    if not math.isfinite(prediction) or prediction > hours * 3600:
        raise PilotGateError("单卡实验预测超过本次实际预算", 3)


def write_pilot_lock(context, execution, trial):
    """将唯一原始进展项锁定到当前运行目录。"""
    evidence = {
        "trial": bound_record(
            Path(trial["final_snapshot_path"]).with_name("trial.json")
        ),
        "target_execution_hash": context["protocol"]["target_execution_hash"],
    }
    lock = lock_pilot_candidate(evidence, execution)
    write_json(
        Path(context["run_dir"]) / "pilot-candidate-lock.json",
        lock,
        immutable=True,
    )
    return lock


def run_registered_pilot(context, settings):
    """执行一次预注册阶段调用；不同参数/调用不能覆盖同一账目。"""
    from .ara_research_schema import RefinementParameters

    execution = stage_execution(
        context["protocol"], settings.ara_v3.stage_execution_id
    )
    parameters = RefinementParameters.model_validate(execution["parameters"])
    operation = execution["operation"]
    evidence_files = {}
    if operation == "trial":
        trial = context["apply"](
            parameters, execution["initialization_attempt"], "pilot"
        )
        if execution["purpose"] == "progress":
            write_pilot_lock(context, execution, trial)
        if execution["purpose"] == "resource":
            evidence_files["resource"] = _measure_resource_exports(
                context, trial
            )
    else:
        trial = _registered_replay(context, execution, parameters)
    context["budget"].stage_call["trial_path"] = str(
        Path(trial["final_snapshot_path"]).with_name("trial.json")
    )
    evidence_files["trial"] = bound_record(
        context["budget"].stage_call["trial_path"]
    )
    context["budget"].stage_call["evidence_files"] = evidence_files
    context["budget"].finish_stage(trial)
    return {
        "phase": "pilot",
        "research_status": "not_run",
        "pilot": trial,
        "update_status": trial.get("update_status", "none"),
        "readiness_status": "pending",
    }, 0


def _registered_replay(context, execution, parameters):
    from .ara_research_runner import compare_replay

    parent = execution["parent_stage_execution_id"]
    ledger = context["budget"].ledger
    previous = ledger["calls"].get(parent)
    if not previous or previous["state"] != "COMPLETE":
        raise PilotGateError("预注册重放的父执行尚未完整完成")
    original = read_json(previous["trial_path"])
    if digest(original) != previous["evidence_hash"]:
        raise PilotGateError("父执行原始证据被改动")
    if execution["operation"] == "reload":
        return _reload_pilot_snapshot(context, original)
    session = context["apply"].__self__
    session.pilot_parent_id = parent
    try:
        actual = context["apply"](
            parameters, original["attempt"], execution["operation"]
        )
        compare_replay(original, actual)
        return actual
    finally:
        session.pilot_parent_id = None


def _reload_pilot_snapshot(context, original):
    import os
    import time
    import torch
    from .ara_refinement_capture import apply_snapshot, snapshot_factors

    started = time.monotonic()
    if original["worker_pid"] == os.getpid():
        raise PilotGateError("pilot 重载必须在独立进程启动")
    snapshot = _verify_snapshot(original)
    model = context["model"]
    before = snapshot_factors(model.ara_targets)
    try:
        apply_snapshot(
            model.ara_targets, snapshot, original["final_snapshot_hash"]
        )
        scores = context["apply"].__self__.development(model)
        _compare_scores(original["scores"], scores)
        path = Path(context["run_dir"]) / "reload" / "factors.pt"
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(snapshot_factors(model.ara_targets), path)
    finally:
        apply_snapshot(model.ara_targets, before)
    result = {
        "operation": "reload",
        "state": "COMPLETE",
        "protocol_hash": original["protocol_hash"],
        "attempt": original["attempt"],
        "stage_execution_id": original["stage_execution_id"],
        "worker_pid": os.getpid(),
        "source_worker_pid": original["worker_pid"],
        "execution_identity_hash": original["execution_identity_hash"],
        "execution_identity": original["execution_identity"],
        "final_snapshot_path": path.resolve().as_posix(),
        "final_snapshot_hash": original["final_snapshot_hash"],
        "snapshot_file_hash": file_digest(path),
        "scores": scores,
        "audit_accessed": False,
        "probe_match": True,
        "measurements": {
            "reload": {
                "phase": "reload",
                "status": "complete",
                "wallclock_seconds": time.monotonic() - started,
            }
        },
    }
    write_json(path.with_name("trial.json"), result, immutable=True)
    return result


def _measure_resource_exports(context, trial):
    """预注册资源项测量开发长响应和比较导出，不产生正式候选。"""
    from .ara_refinement_capture import measure_research_phase
    from .pro6000_experiment import _export_adapter

    records = []
    context["budget"].check()
    with measure_research_phase(context["model"], "shortlist", records):
        semantics = context["semantic"](trial)
    context["budget"].check()
    with measure_research_phase(context["model"], "export", records):
        exported = _export_adapter(
            context, trial, Path(context["run_dir"]) / "resource"
        )
    path = Path(context["run_dir"]) / "resource-measurements.json"
    write_json(
        path,
        {
            "schema_version": "cara-resource-measurements-v1",
            "execution_identity": trial["execution_identity"],
            "execution_identity_hash": trial["execution_identity_hash"],
            "source_trial": bound_record(
                Path(trial["final_snapshot_path"]).with_name("trial.json")
            ),
            "measurements": records,
            "semantics": semantics,
            "export": exported,
            "eligibility": "comparison_only",
        },
        immutable=True,
    )
    return bound_record(path)


def compare_replay(reference: dict, actual: dict) -> None:
    """从锁定搜索状态比较每次重放，不能只比较两个重放。"""
    import torch

    left, right = map(_load_replay_factors, (reference, actual))
    if reference.get("schema_version") == "cara-research-trial-v3.1":
        from .ara_proposal import compare_effective_factors

        for key in (
            "schema_version",
            "execution_identity_hash",
            "study_execution_hash",
            "pilot_execution_config_hash",
        ):
            if reference[key] != actual.get(key):
                raise ValueError(f"重放逻辑执行身份不匹配：{key}")
        compare_effective_factors(left, right)
    elif left.keys() != right.keys() or any(
        not torch.allclose(left[key], right[key], rtol=1e-5, atol=1e-6)
        for key in left
    ):
        raise ValueError("重放因子与锁定候选不一致")
    _compare_scores(reference["scores"], actual["scores"])
    if len(reference["events"]) != len(actual["events"]):
        raise ValueError("重放层组事件数不一致")
    _compare_replay_events(reference["events"], actual["events"])


def _load_replay_factors(row):
    import torch
    from .ara_refinement_capture import tensor_identity

    if row.get("schema_version") == "cara-research-trial-v3.1":
        return _verify_snapshot(row)
    factors = torch.load(
        row["final_snapshot_path"], map_location="cpu", weights_only=True
    )
    if tensor_identity(factors) != row["final_snapshot_hash"]:
        raise ValueError("重放快照与评分时的权重身份不匹配")
    return factors


def _compare_scores(reference, actual):
    for key, tolerance in (
        ("keywords", 0.0),
        ("first_token_kl", 0.005),
        ("sequence_kl", 0.005),
        ("log_odds", 1e-4),
    ):
        if not all(math.isfinite(row[key]) for row in (reference, actual)):
            raise ValueError("重放评分非有限")
        if abs(reference[key] - actual[key]) > tolerance:
            raise ValueError(f"重放指标漂移超限：{key}")


def _compare_replay_events(reference, actual):
    for left_event, right_event in zip(reference, actual, strict=True):
        if left_event.get("proposal_policy"):
            _compare_backtracking_trajectory(left_event, right_event)
        for key in ("sweep", "block_id", "accepted", "rejection_reason"):
            if left_event[key] != right_event[key]:
                raise ValueError(f"重放层组事件不同：{key}")
        for key in (
            "protocol_hash",
            "base_identity",
            "sequences",
            "blocks",
            "steps",
        ):
            if digest(left_event["bank_manifest"][key]) != digest(
                right_event["bank_manifest"][key]
            ):
                raise ValueError(f"重放捕获结构身份不同：{key}")


def _compare_backtracking_trajectory(left, right):
    for key in ("proposal_policy", "selected_alpha"):
        if left[key] != right.get(key):
            raise ValueError(f"重放回溯轨迹不一致：{key}")
    trajectories = [
        [
            (row["alpha"], row["accepted"], row["reasons"])
            for row in event["backtracking_attempts"]
        ]
        for event in (left, right)
    ]
    if trajectories[0] != trajectories[1]:
        raise ValueError("重放各次拒绝原因不同")


def main():
    """离线派生机器可读放行记录；不加载模型或访问审计正文。"""
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = build_pilot_readiness(
            read_json(args.evidence), read_json(args.contract)
        )
        code = 0 if result["status"] == "passed" else 4
    except (ValueError, KeyError, TypeError, OSError) as error:
        code = error.exit_code if isinstance(error, PilotGateError) else 2
        result = {
            "schema_version": "cara-pilot-readiness-v1",
            "status": "inconclusive" if code == 5 else "failed",
            "reason": str(error),
        }
    write_json(args.output, result, immutable=True)
    print(
        json.dumps(
            {
                "phase": "pilot-readiness",
                "research_status": "not_run",
                "result": result,
                "exit_code": code,
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(code)


if __name__ == "__main__":
    main()
