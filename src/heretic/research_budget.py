# SPDX-License-Identifier: AGPL-3.0-or-later
"""研究预算计费、阶段共享账本及 GPU 子进程超时回收。"""

from __future__ import annotations

import os
import math
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .ara_research_schema import digest, exclusive_lock, read_json, write_json

_TIMEOUT_LOCK = threading.Lock()


def campaign_search_status(protocol, identity, study=None, phase="search"):
    """按冻结主方法记录全零停止；失败基线不阻断其他成员。"""
    if protocol.get("schema_version") != "cara-research-protocol-v3.1":
        return
    path, member = _campaign_location(protocol, identity, phase)
    primary = "S2/spectral-backtrack-v1/42"
    with exclusive_lock(path.with_suffix(".lock")):
        ledger = _campaign_ledger(path, protocol)
        if study is None:
            _check_target_admission(
                ledger, member, primary if phase == "search" else None
            )
            return
        rows = [row for row in study["trials"] if row["state"] == "COMPLETE"]
        zero = bool(rows) and all(
            row.get("update_status") == "none" for row in rows
        )
        previous = ledger["members"].get(member, {})
        if (
            previous.get("study_hash", study["study_hash"])
            != study["study_hash"]
        ):
            raise ValueError("阶段成员不能覆盖另一 study 的结果")
        failure = next(
            (row for row in study["trials"] if _blocking_trial(row)), None
        )
        ledger["members"][member] = {
            **previous,
            "study_hash": study["study_hash"],
            "all_zero_update": zero,
            "state": "FAIL" if failure else "COMPLETE",
            "effect_status": study.get("effect_status", "not_run"),
        }
        if failure:
            ledger["blocking_failure"] = {
                "member": member,
                "reason": failure["failure_reason"],
            }
        if member == primary and zero:
            for seed in (43, 44):
                ledger["members"][f"S2/spectral-backtrack-v1/{seed}"] = {
                    "state": "not_run",
                    "reason": "首个目标 study 全部零更新",
                }
        write_json(path, ledger)


def _check_target_admission(ledger, member, primary):
    from .ara_pilot import PilotGateError

    if ledger.get("blocking_failure"):
        raise PilotGateError("共享阶段有尚未解除的资源或身份故障", 3)
    first = ledger["members"].get(primary)
    if primary is None or member == primary:
        return
    if not first or first.get("state") != "COMPLETE":
        raise PilotGateError("主矩阵必须先完成目标方法 seed42")
    if member.startswith("S2/spectral-backtrack-v1/"):
        if first and first.get("all_zero_update"):
            raise PilotGateError("目标首个 study 全零，停止扩大后续 seeds", 4)


class ResearchBudgetExceeded(RuntimeError):
    """累计预算不足或独立 watchdog 到期。"""


def _campaign_location(protocol, identity, phase):
    from .ara_refinement_config import frozen_ledger_name

    budget = protocol["target_execution_contract"]["phase_budgets"][phase]
    path = Path(frozen_ledger_name(budget["ledger_path"]))
    if not path.is_absolute():
        raise ValueError("账本绝对路径不属于当前运行平台")
    member = (
        f"{identity['method_id']}/{identity['proposal_policy']}/"
        f"{identity['seed']}"
    )
    return path, member


def _campaign_ledger(path, protocol):
    ledger = (
        read_json(path)
        if path.exists()
        else {
            "target_execution_hash": protocol["target_execution_hash"],
            "members": {},
        }
    )
    if ledger["target_execution_hash"] != protocol["target_execution_hash"]:
        raise ValueError("主矩阵共享记录目标身份不匹配")
    _latch_campaign_timeouts(ledger, path)
    return ledger


def _latch_campaign_timeouts(ledger, path):
    """watchdog 直接退出时，下一次准入仍须持久化整个阶段的阻断。"""
    for member, row in ledger["members"].items():
        if not row.get("budget_path"):
            continue
        timeout = Path(row["budget_path"]).with_name("budget-timeout.json")
        if not timeout.is_file():
            continue
        evidence = read_json(timeout)
        if evidence.get("reason") == "hard_wallclock_limit":
            row.update(state="FAIL", reason="hard_wallclock_limit")
            ledger["blocking_failure"] = {
                "member": member,
                "reason": "hard_wallclock_limit",
            }
            write_json(path, ledger)
            return


def reserve_campaign_member(protocol, identity, root, phase="search"):
    """原子登记唯一成员目录；同目录恢复沿用预算，换目录不能加试。"""
    root = Path(root).resolve()
    budget = root / "budget.json"
    if protocol.get("schema_version") != "cara-research-protocol-v3.1":
        return budget
    path, member = _campaign_location(protocol, identity, phase)
    binding = {
        "study_hash": digest(identity),
        "run_dir": root.as_posix(),
        "budget_path": budget.as_posix(),
    }
    with exclusive_lock(path.with_suffix(".lock")):
        ledger = _campaign_ledger(path, protocol)
        _check_target_admission(
            ledger,
            member,
            "S2/spectral-backtrack-v1/42" if phase == "search" else None,
        )
        previous = ledger["members"].get(member)
        if previous:
            if any(
                previous.get(key) != value for key, value in binding.items()
            ):
                raise ValueError("同一目标成员已绑定其他运行目录或 study")
            if not budget.is_file():
                raise ValueError("已登记成员缺少原累计预算，禁止重新领取")
            if (
                previous.get("study_path")
                and not Path(previous["study_path"]).is_file()
            ):
                raise ValueError("已开始成员缺少原 study，禁止重新领取尝试次数")
            return budget
        if (root / "study.json").exists() and not budget.exists():
            raise ValueError("已有 study 缺少累计预算，禁止重新领取")
        if not budget.exists():
            write_json(budget, {"sessions": []}, immutable=True)
        ledger["members"][member] = {**binding, "state": "RUNNING"}
        write_json(path, ledger)
    return budget


def record_campaign_study(context, path):
    """首次 study 落盘后记录存在性，恢复时不能通过删文件清零尝试。"""
    protocol = context.get("protocol", {})
    if protocol.get("schema_version") != "cara-research-protocol-v3.1":
        return
    phase = "experiment" if protocol.get("execution_scope") else "search"
    ledger_path, member = _campaign_location(
        protocol, context["identity"], phase
    )
    with exclusive_lock(ledger_path.with_suffix(".lock")):
        ledger = _campaign_ledger(ledger_path, protocol)
        reservation = ledger["members"][member]
        if reservation["study_hash"] != digest(context["identity"]):
            raise ValueError("study 与成员占位身份不一致")
        reservation["study_path"] = Path(path).resolve().as_posix()
        write_json(ledger_path, ledger)


@contextmanager
def campaign_member(protocol, identity, root, phase="search"):
    """预算分配之前占位，传播的运行/重放/暂存故障永久记录为阻断。"""
    reserve_campaign_member(protocol, identity, root, phase)
    try:
        yield
    except Exception as error:
        if protocol.get("schema_version") == "cara-research-protocol-v3.1":
            path, member = _campaign_location(protocol, identity, phase)
            with exclusive_lock(path.with_suffix(".lock")):
                ledger = _campaign_ledger(path, protocol)
                ledger["members"][member].update(
                    state="FAIL", reason=str(error)
                )
                ledger["blocking_failure"] = {
                    "member": member,
                    "reason": str(error),
                }
                write_json(path, ledger)
        raise


def _blocking_trial(row):
    """保留普通数值失败的健康计数，资源/身份故障另行阻断。"""
    if row.get("state") != "FAIL":
        return False
    category = row.get("failure_category", "")
    reason = row.get("failure_reason", "").lower()
    return category in {
        "MemoryError",
        "OutOfMemoryError",
        "ResearchBudgetExceeded",
        "SnapshotRestoreError",
        "TrajectoryRuntimeGuardError",
        "InterruptedWorker",
        "IncompleteStage",
    } or any(
        word in reason
        for word in (
            "out of memory",
            "cuda",
            "cudnn",
            "cublas",
            "device",
            "显存",
            "cpu rss",
            "磁盘不足",
            "身份",
            "hash",
            "fingerprint",
            "协议",
        )
    )


def phase_budget_path(settings, protocol, phase, root):
    """pilot/消融按协议共享总账，正式搜索保留独立 study 总账。"""
    if protocol.get("schema_version") == "cara-research-protocol-v3.1":
        if phase == "pilot":
            from .ara_pilot import stage_execution

            execution = stage_execution(
                protocol, settings.ara_v3.stage_execution_id
            )
            budget = protocol["target_execution_contract"]["phase_budgets"][
                execution["budget_key"]
            ]
            from .ara_refinement_config import frozen_ledger_name

            path = Path(frozen_ledger_name(budget["ledger_path"]))
            if not path.is_absolute():
                raise ValueError("账本绝对路径不属于当前运行平台")
            return path
    is_ablation = settings.ara_v3.method_id in {"A1", "A2", "F1"}
    if phase != "pilot" and not is_ablation:
        return Path(root) / "budget.json"
    name = "pilot" if phase == "pilot" else "ablation"
    return (
        Path(settings.ara_v3.protocol_manifest).parent
        / "phase-budgets"
        / protocol["protocol_hash"]
        / f"{name}.json"
    )


def _terminate_descendants():
    import psutil

    # 只取本进程的后代；psutil 的实例身份检查避免 PID 复用误杀。
    descendants = psutil.Process(os.getpid()).children(recursive=True)
    terminated, errors = [], []
    for process in descendants:
        try:
            process.kill()
            terminated.append(process.pid)
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as error:
            errors.append({"pid": process.pid, "reason": str(error)})
    _, alive = psutil.wait_procs(descendants, timeout=3)
    return {
        "terminated_descendants": terminated,
        "unreaped_descendants": [process.pid for process in alive],
        "termination_errors": errors,
    }


class StudyBudget:
    """累计所有启动区间，独立计时器不会覆盖嵌套阶段预算。"""

    def __init__(self, path, limit_seconds=28800.0, devices=2, clock=time.time):
        self.path, self.limit, self.devices, self.clock = (
            Path(path),
            limit_seconds,
            devices,
            clock,
        )
        self.ledger = (
            read_json(path) if Path(path).exists() else {"sessions": []}
        )
        for session in self.ledger["sessions"]:
            if session.get("stop") is None:
                session["stop"] = min(clock(), session["deadline"])
                session["interrupted"] = True
                session["stop_estimated"] = True
        for call in self.ledger.get("calls", {}).values():
            if call["state"] == "RUNNING":
                call.update(
                    state="FAIL",
                    reason="orphaned_worker",
                    stopped_at=clock(),
                    failure_category="InterruptedWorker",
                    failure_reason="orphaned_worker",
                )
        self.session = None
        self._state_lock = threading.RLock()

    def bind_stage(self, protocol, execution):
        """绑定 campaign/stage 和冻结调用，目录变化不能重新领取预算。"""
        identity = {
            "campaign_id": execution["campaign_id"],
            "stage": execution["stage"],
            "target_execution_hash": protocol["target_execution_hash"],
        }
        for name, expected in identity.items():
            if name in self.ledger and self.ledger[name] != expected:
                raise ValueError("阶段预算身份不一致")
        self.ledger.update(identity)
        calls = self.ledger.setdefault("calls", {})
        key = execution["stage_execution_id"]
        if key in calls:
            raise ValueError("阶段调用已有完成或故障记录，禁止重复加试")
        calls[key] = {
            "execution_hash": digest(execution),
            "source_protocol_hash": protocol["protocol_hash"],
            "state": "RUNNING",
            "started_at": self.clock(),
            "session_index": len(self.ledger["sessions"]) - 1,
        }
        self.session["stage_execution_id"] = key
        self.stage_call = calls[key]
        write_json(self.path, self.ledger)

    def finish_stage(self, evidence):
        """只有实际产生证据后完成调用，保存输出身份。"""
        self.stage_call.update(
            state="COMPLETE",
            stopped_at=self.clock(),
            evidence_hash=digest(evidence),
        )
        write_json(self.path, self.ledger)

    def elapsed(self):
        """计算跨启动累计占用墙钟，未知退出时间采用保守估计。"""
        return sum(
            max(0.0, (row.get("stop") or self.clock()) - row["start"])
            for row in self.ledger["sessions"]
        )

    def __enter__(self):
        """在模型分配前开始计费，启动不依赖主线程信号的硬上限。"""
        remaining = self.limit - self.elapsed()
        if remaining <= 0:
            write_json(self.path, self.ledger)
            raise ResearchBudgetExceeded("累计 study 墙钟预算已用尽")
        now = self.clock()
        self.session = {
            "start": now,
            "stop": None,
            "devices": self.devices,
            "deadline": now + remaining,
            "pid": os.getpid(),
        }
        self.ledger["sessions"].append(self.session)
        write_json(self.path, self.ledger)
        self.watchdog = threading.Timer(remaining, self._hard_timeout)
        self.watchdog.daemon = True
        self.watchdog.start()
        return self

    def _hard_timeout(self):
        # 嵌套预算同时到期时，仅由一个监督线程回收进程并记录退出。
        with self._state_lock:
            if self.session["stop"] is not None:
                return
            if not _TIMEOUT_LOCK.acquire(blocking=False):
                return
            try:
                termination = _terminate_descendants()
                self.session["interrupted"] = True
                self._fail_stage(ResearchBudgetExceeded("hard_wallclock_limit"))
                self._finish_session()
                write_json(
                    self.path.with_name("budget-timeout.json"),
                    {
                        "status": "failed",
                        "reason": "hard_wallclock_limit",
                        "worker_pid": os.getpid(),
                        **termination,
                    },
                )
            finally:
                os._exit(3)

    def check(self):
        """在 attempt/层组边界校验剩余预算。"""
        if self.elapsed() >= self.limit:
            raise ResearchBudgetExceeded("study 累计墙钟预算不足")

    def _finish_session(self):
        self.session["stop"] = self.clock()
        self.ledger["gpu_hours"] = sum(
            (row["stop"] - row["start"]) * row["devices"] / 3600
            for row in self.ledger["sessions"]
        )
        write_json(self.path, self.ledger)

    def __exit__(self, *exception):
        with self._state_lock:
            self._fail_stage(exception[1])
            self.watchdog.cancel()
            self._finish_session()

    def _fail_stage(self, error):
        call = getattr(self, "stage_call", None)
        if call and call["state"] == "RUNNING":
            call.update(
                state="FAIL",
                stopped_at=self.clock(),
                failure_category=type(error).__name__
                if error
                else "IncompleteStage",
                failure_reason=str(error) if error else "阶段未完成",
            )


def completed_stage_evidence(budget, protocol, execution):
    """恢复已完成调用只读原证据；孤立或失败调用永不重新执行。"""
    from .ara_pilot import _verify_snapshot

    call = budget.ledger.get("calls", {}).get(execution["stage_execution_id"])
    if call is None:
        return None
    if (
        budget.ledger["target_execution_hash"]
        != protocol["target_execution_hash"]
        or call["source_protocol_hash"] != protocol["protocol_hash"]
        or call["execution_hash"] != digest(execution)
    ):
        raise ValueError("只读恢复阶段身份不匹配")
    if call["state"] != "COMPLETE":
        write_json(budget.path, budget.ledger)
        raise ResearchBudgetExceeded("已失败或中断的阶段调用禁止重试")
    trial = read_json(call["trial_path"])
    if digest(trial) != call["evidence_hash"]:
        raise ValueError("已完成阶段的证据被修改")
    _verify_snapshot(trial)
    return {
        "phase": "pilot",
        "research_status": "not_run",
        "pilot": trial,
        "update_status": trial.get("update_status", "none"),
        "readiness_status": "pending",
        "recovered_read_only": True,
    }, 0


def run_budgeted_phase(settings, protocol, root, phase, limit):
    from .ara_research_schema import study_identity

    if phase == "search":
        with campaign_member(
            protocol, study_identity(settings, protocol), root
        ):
            return _run_charged_phase(settings, protocol, root, phase, limit)
    return _run_charged_phase(settings, protocol, root, phase, limit)


def _run_charged_phase(settings, protocol, root, phase, limit):
    """在共享锁内恢复、计费和分配模型，恢复完成项不启动 watchdog。"""
    from .ara_pilot import stage_execution
    from .ara_research_runner import _runtime_context, _execute_model_phase

    path = phase_budget_path(settings, protocol, phase, root)
    is_new = protocol.get("schema_version", "").endswith("-v3.1")
    if is_new and phase == "search":
        completed = recover_completed_search(settings, protocol, root)
        if completed:
            return completed
    devices = (
        len(settings.ara_runtime_guard.required_target_devices) if is_new else 2
    )
    with exclusive_lock(path.with_suffix(".lock")):
        budget = StudyBudget(path, limit, devices=devices)
        execution = None
        if phase == "pilot" and is_new:
            execution = stage_execution(
                protocol, settings.ara_v3.stage_execution_id
            )
            recovered = completed_stage_evidence(budget, protocol, execution)
            if recovered:
                return recovered
        with budget:
            if execution:
                budget.bind_stage(protocol, execution)
                from .ara_pilot import bound_record

                budget.stage_call["source_protocol"] = bound_record(
                    Path(root) / "protocol.json"
                )
            context = _runtime_context(settings, protocol, root, budget)
            return _execute_model_phase(context, settings, phase)


def recover_completed_search(settings, protocol, root):
    """完整研究已进入 staging 后仅核验冻结制品，不再次加载或计费。"""
    from .ara_research_schema import study_identity
    from .research_audit import _verify_staging

    member_path = Path(root) / "member.json"
    if not member_path.exists():
        return None
    study = read_json(Path(root) / "study.json")
    if study["study_hash"] != digest(study_identity(settings, protocol)):
        raise ValueError("只读恢复 study 身份不匹配")
    if len(study["trials"]) != 24 or any(
        row["state"] not in {"COMPLETE", "FAIL"} for row in study["trials"]
    ):
        raise ValueError("已暂存成员缺少完整终态 study")
    member = read_json(member_path)
    if member["candidate_lock_hash"] != digest(study["candidate_lock"]):
        raise ValueError("已暂存成员候选锁不匹配")
    _verify_staging(member)
    campaign_search_status(protocol, study_identity(settings, protocol), study)
    status = study["effect_status"]
    return {
        "phase": "search",
        "recovered_read_only": True,
        "research_status": "inconclusive" if status == "passed" else "failed",
    }, (0 if status == "passed" else 4)


def write_cost_checkpoints(study, current_hours):
    from .ara_research_runner import _trial_sort

    checkpoints = study.setdefault("gpu_hours_checkpoints", {})
    if study.get("schema_version") == "cara-research-study-v3.1":
        study["cost_observation"] = {
            "actual_gpu_hours": current_hours,
            "terminal_attempts": sum(
                row["state"] in {"COMPLETE", "FAIL"} for row in study["trials"]
            ),
            "unreached_checkpoints": [
                point for point in (4, 8, 12, 16) if point > current_hours
            ],
        }
    for checkpoint in (4, 8, 12, 16):
        if current_hours < checkpoint or str(checkpoint) in checkpoints:
            continue
        eligible = [
            row
            for row in study["trials"]
            if row["state"] == "COMPLETE"
            and row["completed_gpu_hours"] <= checkpoint
        ]
        best = min(eligible, key=_trial_sort) if eligible else None
        checkpoints[str(checkpoint)] = None if best is None else best["attempt"]


def _cost_provenance(contract, ledger):
    from .ara_pilot import PilotGateError, _verify_stage_ledger
    from .ara_research_schema import validate_target_contract, StageExecution

    target = validate_target_contract(contract)
    if (
        not ledger
        or ledger.get("target_execution_hash") != target
        or ledger.get("stage") != "R2"
    ):
        raise PilotGateError("资源证据缺少本目标已结清的 R2 共享账本", 5)
    execution = next(
        row
        for row in contract["stage_executions"]
        if row["stage_execution_id"] == "r2-backtrack-anchor0"
    )
    _verify_stage_ledger(
        ledger,
        contract,
        StageExecution.model_validate(execution).model_dump(mode="json"),
    )
    return {"contract": contract, "ledger": ledger}


def pilot_cost_prediction(resource, contract, ledger=None):
    from .ara_pilot import PilotGateError

    provenance = _cost_provenance(contract, ledger)
    budgets = {}
    for phase in ("search", "experiment"):
        for member, budget in (
            contract["phase_budgets"].get(phase, {}).get("members", {}).items()
        ):
            budgets.setdefault(member, []).append(budget)
    if not budgets:
        raise PilotGateError("目标没有冻结 search/experiment 成员预算", 5)
    if set(resource.get("members", {})) != set(budgets):
        raise PilotGateError("资源证明没有覆盖每个目标成员的独立求解器", 5)
    predictions = {}
    for member, limits in budgets.items():
        values = _measured_cost(resource["members"][member], member, provenance)
        predicted = 1.25 * (
            values["load"]
            + 24 * values["trial_bound"]
            + values["shortlist_bound"]
            + 2 * values["replay_bound"]
            + values["apply_reload_export_bound"]
        )
        ceiling = min(
            min(
                b["max_wallclock_seconds"],
                b["max_gpu_hours"] * 3600 / b["devices"],
            )
            for b in limits
        )
        if predicted > ceiling:
            raise PilotGateError("资源预测超过冻结扩大预算", 3)
        predictions[member] = {"predicted_seconds": predicted, "inputs": values}
    return {
        "members": predictions,
        "formula": (
            "1.25*(load+24*trial+shortlist+2*replay+apply_reload_export)"
        ),
    }


def _measured_cost(terms, member, provenance):
    from .ara_pilot import PilotGateError

    stages = (
        "load",
        "trial_bound",
        "shortlist_bound",
        "replay_bound",
        "apply_reload_export_bound",
    )
    if set(terms) != set(stages):
        raise PilotGateError("资源测量缺少必经阶段", 5)
    values = {}
    for name, row in terms.items():
        samples = [
            _measurement_seconds(item, member, provenance)
            for item in row["observations"]
        ]
        multiplier = row["workload_multiplier"]
        if not samples or not all(math.isfinite(x) and x > 0 for x in samples):
            raise PilotGateError("资源测量缺失、非有限或伪填零", 5)
        if not math.isfinite(multiplier) or multiplier < 1:
            raise PilotGateError("资源外推倍率非法")
        if not row.get("workload") or not row.get("covered_branches"):
            raise PilotGateError("资源缺少工作量与必经分支测量", 5)
        _validate_measured_branches(name, row, member)
        if name == "trial_bound":
            _validate_trial_multiplier(row, member)
        phase_maxima = {}
        for observation, seconds in zip(
            row["observations"], samples, strict=True
        ):
            phase = observation["phase"]
            phase_maxima[phase] = max(seconds, phase_maxima.get(phase, 0))
        seconds = (
            sum(phase_maxima.values())
            if name == "apply_reload_export_bound"
            else max(samples)
        )
        if name == "shortlist_bound" and multiplier < 3:
            raise PilotGateError("shortlist 成本必须覆盖三个候选", 5)
        values[name] = seconds * multiplier
    return values


def _measurement_seconds(observation, member, provenance):
    from .ara_pilot import PilotGateError
    from .ara_refinement_config import validate_cost_source

    source = validate_cost_source(observation, member, provenance)
    identity = source["execution_identity"]
    if digest(identity) != source["execution_identity_hash"]:
        raise PilotGateError("资源原始执行身份摘要不匹配")
    method, policy, _ = member.split("/")
    if identity["method_id"] != method or identity["proposal_policy"] != policy:
        raise PilotGateError("不能用另一求解器的成本替代本成员")
    record = source
    for key in observation["record_path"]:
        record = record[key]
    if record.get("status") != "complete" or "wallclock_seconds" not in record:
        raise PilotGateError("资源引用没有完整的原生阶段计时", 5)
    if record["phase"] != observation["phase"]:
        raise PilotGateError("资源引用阶段不匹配")
    return record["wallclock_seconds"]


def _measured_phases(value):
    """只认可原生成功阶段，不将摘要内人工分支名字当作测量。"""
    phases = set()
    if isinstance(value, dict):
        seconds = value.get("wallclock_seconds", 0)
        if (
            value.get("status") == "complete"
            and value.get("phase")
            and isinstance(seconds, (int, float))
            and seconds > 0
        ):
            phases.add(value["phase"])
        for child in value.values():
            phases.update(_measured_phases(child))
    elif isinstance(value, list):
        for child in value:
            phases.update(_measured_phases(child))
    return phases


def _validate_measured_branches(name, row, member):
    from .ara_pilot import read_bound, PilotGateError

    phases = {item.get("phase") for item in row["observations"]}
    expected = {
        "load": {"load"},
        "trial_bound": {"pilot"},
        "shortlist_bound": {"shortlist"},
        "replay_bound": {"replay-1", "replay-2"},
        "apply_reload_export_bound": {"third-apply", "reload", "export"},
    }[name]
    if phases != expected:
        raise PilotGateError("计费引用没有覆盖成本项的完整操作", 5)
    observed = set()
    for observation in row["observations"]:
        observed.update(_measured_phases(read_bound(observation["source"])))
    required = {
        "load": {"load"},
        "shortlist_bound": {"shortlist"},
        "replay_bound": {"replay-1", "replay-2"},
        "apply_reload_export_bound": {"third-apply", "reload", "export"},
        "trial_bound": {
            "capture",
            "local_optimization",
            "keywords",
            "prefix_log_odds",
            "sequence_kl",
        },
    }[name]
    if name == "trial_bound" and member.split("/")[0] not in {"B1", "B2"}:
        required |= {"actual_forward", "monitor_after"}
    if not required.issubset(observed) or not set(
        row["covered_branches"]
    ).issubset(observed):
        raise PilotGateError("必经昂贵分支没有原生成功测量", 5)


def _validate_trial_multiplier(row, member):
    from .ara_pilot import read_bound, PilotGateError
    from .ara_refinement_config import METHODS

    method, policy, _ = member.split("/")
    for item in row["observations"]:
        source = read_bound(item["source"])
        work = source.get("workload_evidence", {})
        required = {
            "target_modules",
            "observed_modules",
            "sweeps",
            "max_backtracks",
        }
        if not required.issubset(work) or any(work[k] <= 0 for k in required):
            raise PilotGateError("trial 成本缺少原生模块/轮次/回溯工作量", 5)
        multiplier = max(1, work["target_modules"] / work["observed_modules"])
        multiplier *= max(1, METHODS[method][1] / work["sweeps"])
        backtracks = 5 if policy == "spectral-backtrack-v1" else 1
        multiplier *= max(1, backtracks / work["max_backtracks"])
        if row["workload_multiplier"] < multiplier:
            raise PilotGateError("trial 外推没有覆盖全部模块、轮次和回溯", 5)
