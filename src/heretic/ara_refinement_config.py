# SPDX-License-Identifier: AGPL-3.0-or-later
"""顺序重校准研究的冻结配置；不复用历史搜索预算。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

METHODS = {
    "B1": ("point-reference", 1, "base-fixed", "legacy", "frozen-reference"),
    "B2": (
        "trajectory-reference",
        1,
        "base-fixed",
        "legacy",
        "frozen-reference",
    ),
    "S1": ("sequential-fixed", 1, "base-fixed", "base-anchored", "sequential"),
    "S2": (
        "sequential-refresh",
        2,
        "refresh-each-sweep",
        "base-anchored",
        "sequential",
    ),
    "A1": ("sequential-fixed", 2, "base-fixed", "base-anchored", "sequential"),
    "A2": (
        "local-keep-ablation",
        2,
        "refresh-each-sweep",
        "local-update",
        "sequential",
    ),
    "F1": (
        "sequential-fixed",
        1,
        "base-fixed",
        "base-anchored",
        "frozen-diagnostic",
    ),
}
METHOD_FIELDS = (
    "variant",
    "sweeps",
    "trajectory_policy",
    "keep_reference",
    "capture_mode",
)
PARAMETER_RANGES = {
    "layer_start": (0.15, 0.50),
    "layer_span": (0.35, 0.75),
    "attn_strength": (0.05, 4.0),
    "mlp_strength": (0.0001, 2.0),
    "push_weight": (0.5, 6.0),
    "margin": (2.0, 16.0),
}
ANCHORS = (
    (
        0.2537726751,
        0.5499400281,
        0.8642483051,
        0.1177926877,
        1.8111605268,
        3.6525113958,
    ),
    (0.2537726751, 0.5499400281, 0.8642483051, 0.1177926877, 3.0, 8.0),
    (0.15, 0.75, 0.8642483051, 0.1177926877, 1.8111605268, 3.6525113958),
    (0.30, 0.50, 0.50, 0.05, 1.50, 4.0),
)
ROLE_COUNTS = {
    "fit": 192,
    "monitor": 64,
    "mechanism-development": 44,
    "development": 100,
    "diagnostic": 16,
}


class RefinementConfig(BaseModel):
    """通过合法组合唯一确定方法身份，拒绝未注册实验。"""

    model_config = {"extra": "forbid", "allow_inf_nan": False}
    variant: str = "sequential-refresh"
    sweeps: Literal[1, 2] = 2
    trajectory_policy: str = "refresh-each-sweep"
    keep_reference: str = "base-anchored"
    capture_mode: str = "sequential"
    block_layers: Literal[8] = 8
    trajectory_tokens: Literal[8] = 8
    sequence_kl_tokens: Literal[32] = 32
    trial_budget: Literal[24] = 24
    monitor_samples: Literal[64] = 64
    protocol_manifest: str = Field(min_length=1)
    gpu_hours_checkpoints: tuple[float, ...] = (4.0, 8.0, 12.0, 16.0)
    artifact_schema: Literal[
        "cara-research-acceptance-v3", "cara-research-acceptance-v3.1"
    ] = "cara-research-acceptance-v3"
    required_level: Literal[
        "recovery", "sample_extreme", "statistical_extreme"
    ] = "statistical_extreme"
    parent_candidate_lock_hash: str | None = None
    parent_candidate_lock_path: str | None = None
    wallclock_seconds: Literal[28800] = 28800
    max_cpu_rss_gib: float = Field(default=64.0, gt=0, le=64)
    proposal_policy: Literal[
        "reject-v1",
        "scale-v1",
        "spectral-clip-v1",
        "spectral-backtrack-v1",
    ] = "reject-v1"
    backtracking_alphas: tuple[float, ...] = (1.0,)
    spectral_projection_margin: Literal[0.000001] = 0.000001
    pilot_profile: Literal["smoke", "full-calibration"] = "smoke"
    stage_execution_id: str | None = None

    @property
    def method_id(self) -> str:
        """返回唯一方法代号；未知组合抛出 ValueError。"""
        combination = tuple(getattr(self, key) for key in METHOD_FIELDS)
        for name, values in METHODS.items():
            if values == combination:
                return name
        raise ValueError("ara_v3 方法组合未注册")

    @model_validator(mode="after")
    def validate_contract(self) -> "RefinementConfig":
        """在模型分配前校验方法及成本检查点。"""
        method = self.method_id
        if method in {"A1", "A2", "F1"}:
            if not self.parent_candidate_lock_hash:
                raise ValueError("消融必须绑定 parent_candidate_lock_hash")
        if self.gpu_hours_checkpoints != (4.0, 8.0, 12.0, 16.0):
            raise ValueError("正式成本检查点固定为 4/8/12/16 GPU-hours")
        expected = (1.0,)
        if self.proposal_policy == "spectral-backtrack-v1":
            expected = (1.0, 0.5, 0.25, 0.125, 0.0625)
        if self.backtracking_alphas != expected:
            raise ValueError("回溯 alpha 必须使用策略的固定序列")
        if method in {"B1", "B2"} and self.proposal_policy != "reject-v1":
            raise ValueError("旧求解器对照仅允许 reject-v1")
        if self.artifact_schema.endswith("-v3"):
            if self.proposal_policy != "reject-v1":
                raise ValueError("新策略必须使用 v3.1 制品")
            if self.pilot_profile != "smoke" or self.stage_execution_id:
                raise ValueError("新试点字段不能写入旧制品")
        return self


def validate_refinement_settings(settings) -> None:
    """验证 v3 外层设置，避免落入旧 gate 或随机生成路径。"""
    if settings.ara_objective_version != "sequential-v3":
        if settings.ara_v3 is not None:
            raise ValueError("ara_v3 仅用于 sequential-v3")
        return
    if settings.ara_v3 is None or settings.abliteration_method != "ara":
        raise ValueError("sequential-v3 必须配置 ARA 和 ara_v3")
    if settings.acceptance_gate is not None:
        raise ValueError("v3 禁止使用历史 AcceptanceGate")
    if settings.ara_lora_rank != 128 or settings.ara_capture_batch_size != 1:
        raise ValueError("v3 固定 rank=128、capture batch=1")
    if settings.seed not in {42, 43, 44}:
        raise ValueError("v3 必须指定预注册 seed 42/43/44")
    if settings.n_trials != 24 or settings.n_startup_trials != 8:
        raise ValueError("v3 使用 4+8+12，n_trials=24、n_startup_trials=8")
    _validate_generation(settings)
    _validate_fixed_runtime(settings)


def _validate_generation(settings):
    if settings.response_prefix not in (None, ""):
        raise ValueError("v3 禁止非空 response_prefix，不能预填回答")
    if settings.chat_template_kwargs.get("enable_thinking") is not False:
        raise ValueError("v3 必须显式关闭 thinking")
    generation = settings.generation_kwargs
    if generation.get("do_sample", False) is not False:
        raise ValueError("v3 必须关闭采样")
    if generation.get("num_beams", 1) != 1:
        raise ValueError("v3 必须使用 num_beams=1")
    if settings.max_response_length != 100:
        raise ValueError("v3 Keywords 固定使用 100-token 预算")


def _validate_fixed_runtime(settings):
    expected = {
        "ara_calibration_size": 96,
        "ara_trajectory_tokens": 8,
        "ara_trajectory_decay": 0.85,
        "ara_softmin_temperature": 0.10,
        "ara_lbfgs_max_iter": 20,
        "ara_lbfgs_history_size": 10,
        "ara_max_good_delta_rms": 0.60,
        "ara_max_singular_value": 8.0,
    }
    for key, value in expected.items():
        if getattr(settings, key) != value:
            raise ValueError(f"v3 固定 {key}={value}")
    if settings.ara_runtime_guard is None:
        raise ValueError("v3 必须配置实际模块、设备及资源 guard")


def load_refinement_settings(path):
    """从指定 TOML 构造现有 Settings，不把阶段参数传给旧解析器。"""
    import sys
    import tomllib
    from pathlib import Path
    from .config import Settings

    with Path(path).open("rb") as stream:
        values = tomllib.load(stream)
    original = sys.argv
    try:
        sys.argv = ["heretic"]
        return Settings.model_validate(values)
    finally:
        sys.argv = original


def research_phase_limit(settings, protocol, phase):
    """模型分配前要求阶段预算及正式矩阵的 pilot 资源证明。"""
    from .ara_research_schema import file_digest, read_json

    if protocol.get("schema_version") == "cara-research-protocol-v3.1":
        return _new_phase_limit(settings, protocol, phase)

    method = settings.ara_v3.method_id
    is_ablation = method in {"A1", "A2", "F1"}
    if phase == "pilot" or is_ablation:
        name = "pilot" if phase == "pilot" else "ablation"
        budget = protocol["phase_budgets"].get(name)
        if not budget or not budget.get("members"):
            raise ValueError(f"{name} 缺少冻结预算或成员清单")
        members = {
            row if isinstance(row, str) else row.get("member_id")
            for row in budget["members"]
        }
        if f"{method}-{settings.seed}" not in members:
            raise ValueError(f"{name} 未预注册当前方法/seed 成员")
        wall = budget["max_wallclock_seconds"]
        gpu_wall = budget["max_gpu_hours"] * 3600 / 2
        if min(wall, gpu_wall) <= 0:
            raise ValueError("阶段预算必须为正")
        return min(wall, gpu_wall)
    proof = protocol["phase_budgets"].get("search", {}).get("pilot_evidence")
    if not proof or file_digest(proof["path"]) != proof["sha256"]:
        raise ValueError("正式搜索前必须固定真实 pilot 证据")
    pilot = read_json(proof["path"])
    if pilot.get("status") != "passed":
        raise ValueError("pilot 资源门槛未通过")
    predicted = pilot["predicted_slowest_study_seconds"]
    if not 0 < predicted * 1.25 <= settings.ara_v3.wallclock_seconds:
        raise ValueError("pilot 估计加 25% 重放余量超过正式预算")
    return settings.ara_v3.wallclock_seconds


def _new_phase_limit(settings, protocol, phase):
    from .ara_pilot import stage_execution, validate_phase_readiness

    validate_phase_readiness(protocol, phase)
    if phase == "pilot":
        execution = stage_execution(
            protocol, settings.ara_v3.stage_execution_id
        )
        member = (
            f"{settings.ara_v3.method_id}/"
            f"{settings.ara_v3.proposal_policy}/{settings.seed}"
        )
        if execution["member_id"] != member:
            raise ValueError("阶段执行项与当前方法/策略/seed 不匹配")
        if execution["profile"] != settings.ara_v3.pilot_profile:
            raise ValueError("阶段执行项校准 profile 不匹配")
        budget = protocol["target_execution_contract"]["phase_budgets"][
            execution["budget_key"]
        ]
    else:
        member = (
            f"{settings.ara_v3.method_id}/"
            f"{settings.ara_v3.proposal_policy}/{settings.seed}"
        )
        budget = protocol["target_execution_contract"]["phase_budgets"][phase][
            "members"
        ].get(member)
        if not budget:
            raise ValueError("目标契约未登记当前搜索方法/策略/seed 成本")
    devices = budget["devices"]
    if devices not in (1, 2):
        raise ValueError("计费设备数必须与实际一张或两张 GPU 对应")
    actual = len(settings.ara_runtime_guard.required_target_devices)
    if actual != devices:
        raise ValueError("预算 GPU 数与实际 placement 不一致")
    limit = min(
        budget["max_wallclock_seconds"],
        budget["max_gpu_hours"] * 3600 / devices,
    )
    if not 0 < limit <= settings.ara_v3.wallclock_seconds:
        raise ValueError("冻结阶段预算必须为正且不超过八小时")
    return limit


def validate_target_settings(contract, executions):
    """预注册数值、角色与共享预算在读取试点结果前固定。"""
    _validate_ledger_paths(contract)
    if contract["parameter_ranges"] != {
        k: list(v) for k, v in PARAMETER_RANGES.items()
    }:
        raise ValueError("目标参数空间不符合固定六字段范围")
    for key in (
        "model_identity",
        "tokenizer_identity",
        "source_files",
        "package_versions",
        "scorer_identity",
        "initialization_sources",
    ):
        if not contract[key]:
            raise ValueError(f"目标执行契约缺少完整身份：{key}")
    _validate_profile_mappings(contract)
    if (
        len(contract["hardware"].get("device_names", []))
        != contract["hardware"]["devices"]
    ):
        raise ValueError("目标契约必须冻结每张 GPU 的实际型号")
    for stage, profile in (("R1", "smoke"), ("R2", "full-calibration")):
        budget = contract["phase_budgets"][stage]
        if (
            budget["max_wallclock_seconds"] != 28800
            or budget["devices"] not in (1, 2)
            or budget["devices"] != contract["hardware"]["devices"]
            or budget["max_gpu_hours"] != 8 * budget["devices"]
            or not budget.get("ledger_path")
        ):
            raise ValueError("R1/R2 必须按实际设备共享八小时阶段预算")
        for row in executions:
            if row.stage == stage and (
                row.profile != profile or row.budget_key != stage
            ):
                raise ValueError("阶段 profile 或共享预算键不匹配")
    rules = {
        "rank": 128,
        "max_singular_value": 8.0,
        "max_cumulative_ratio": 0.60,
        "relative_change": 1e-6,
        "backtracking_alphas": [1, 0.5, 0.25, 0.125, 0.0625],
    }
    if any(contract["validation_rules"].get(k) != v for k, v in rules.items()):
        raise ValueError("目标契约没有冻结完整数值门槛")


def _validate_profile_mappings(contract):
    profiles = contract["role_mappings"]
    for profile in ("smoke", "full-calibration"):
        mapping = profiles[profile]
        for role in ("fit", "monitor", "development", "mechanism-development"):
            count = (
                8
                if profile == "smoke" and role in {"fit", "monitor"}
                else ROLE_COUNTS[role]
            )
            for side in ("good", "bad"):
                values = mapping[f"{role}.{side}"]
                if len(values) != count or len(set(values)) != count:
                    raise ValueError("冻结 profile 候选 ID 数量或唯一性错误")
        if mapping.get("_fit_selected_ids") != freeze_fit_ids(mapping, profile):
            raise ValueError("目标契约没有冻结每个 seed 的 fit 选中 ID/顺序")
    for role in (
        "development.good",
        "development.bad",
        "mechanism-development.good",
        "mechanism-development.bad",
    ):
        if profiles["smoke"][role] != profiles["full-calibration"][role]:
            raise ValueError("R1/R2 开发角色 ID 和顺序必须保持不变")


def freeze_fit_ids(mapping, profile):
    """沿用共同 seed 抽样规则，在 R1 前列出两个 profile 的选中身份。"""
    import random
    from .ara_research_schema import digest

    count = 8 if profile == "smoke" else 96
    selected = {}
    for seed in (42, 43, 44):
        selected[str(seed)] = {}
        for side in ("good", "bad"):
            ids = mapping[f"fit.{side}"]
            generator = random.Random(int(digest([seed, "fit", side])[:16], 16))
            indices = sorted(generator.sample(range(len(ids)), count))
            selected[str(seed)][side] = [ids[index] for index in indices]
    return selected


def frozen_ledger_name(value):
    """接受冻结的 Linux/Windows 绝对路径，拒绝依赖启动目录的定位。"""
    from pathlib import PurePosixPath, PureWindowsPath

    if not isinstance(value, str) or not value:
        raise ValueError("共享账本必须使用冻结的绝对路径")
    candidates = (PurePosixPath(value), PureWindowsPath(value))
    absolute = [path for path in candidates if path.is_absolute()]
    if not absolute or any(".." in path.parts for path in absolute):
        raise ValueError("共享账本必须使用无父目录跳转的绝对路径")
    return absolute[0].as_posix()


def _validate_ledger_paths(contract):
    names = [
        frozen_ledger_name(contract["phase_budgets"][phase].get("ledger_path"))
        for phase in ("R1", "R2", "search", "experiment")
        if phase in contract["phase_budgets"]
    ]
    if len(names) != len(set(names)):
        raise ValueError("不同阶段必须使用独立的冻结账本")


def validate_cost_source(observation, member, provenance):
    """计时必须来自当前 R2 已计费调用，不能借用其他协议或资源探针。"""
    from .ara_pilot import PilotGateError, read_bound
    from .ara_research_schema import digest

    contract, ledger = provenance["contract"], provenance["ledger"]
    reference = observation["source"]
    matches = [
        (key, call)
        for key, call in ledger["calls"].items()
        if reference in call.get("evidence_files", {}).values()
    ]
    if len(matches) != 1:
        raise PilotGateError("成本来源没有唯一已计费的 R2 调用", 5)
    key, call = matches[0]
    execution = next(
        row
        for row in contract["stage_executions"]
        if row["stage_execution_id"] == key
    )
    _validate_cost_call(call, execution, provenance)
    protocol = read_bound(call["source_protocol"])
    _validate_cost_protocol(protocol, contract)
    trial = read_bound(call["evidence_files"]["trial"])
    if (
        digest(trial) != call["evidence_hash"]
        or trial["protocol_hash"] != protocol["protocol_hash"]
        or call["source_protocol_hash"] != protocol["protocol_hash"]
    ):
        raise PilotGateError("计费调用、来源协议与原始 trial 身份不一致")
    _validate_cost_trial(trial, protocol, execution)
    method, policy, _ = member.split("/")
    if execution["member_id"].split("/")[:2] != [method, policy]:
        raise PilotGateError("资源调用属于另一求解器或策略")
    source = read_bound(reference)
    _validate_cost_operation(observation, execution, call)
    if reference != call["evidence_files"]["trial"]:
        if source.get("source_trial") != call["evidence_files"]["trial"]:
            raise PilotGateError("资源汇总没有绑定已计费原始 trial")
    if source["execution_identity"] != trial["execution_identity"]:
        raise PilotGateError("资源输出与原始 trial 执行身份不一致")
    return source


def _validate_cost_trial(trial, protocol, execution):
    from .ara_pilot import PilotGateError
    from .ara_research_schema import (
        build_execution_identity,
        RefinementParameters,
    )

    if trial.get("state") != "COMPLETE":
        raise PilotGateError("成本原始 trial 尚未完整完成", 5)
    identity = trial["execution_identity"]
    config = RefinementConfig.model_validate(
        identity["execution_config"]["refinement"]
    )
    parent = (
        execution.get("parent_stage_execution_id")
        or execution["stage_execution_id"]
    )
    if config.stage_execution_id != parent:
        raise PilotGateError("成本原始 trial 没有继承预注册执行身份")
    expected = build_execution_identity(
        protocol,
        config,
        int(execution["member_id"].split("/")[2]),
        (
            RefinementParameters.model_validate(execution["parameters"]),
            execution["initialization_attempt"],
        ),
    )
    if identity != expected:
        raise PilotGateError("成本原始 trial 参数、初始化或协议身份不匹配")


def _validate_cost_call(call, execution, provenance):
    from .ara_pilot import PilotGateError
    from .ara_research_schema import StageExecution, digest

    expected = StageExecution.model_validate(execution).model_dump(mode="json")
    if execution["stage"] != "R2" or call["state"] != "COMPLETE":
        raise PilotGateError("成本来源不是已完成 R2 调用", 5)
    if call["execution_hash"] != digest(expected):
        raise PilotGateError("资源调用与预注册参数身份不一致")
    index = call.get("session_index", -1)
    sessions = provenance["ledger"]["sessions"]
    if not isinstance(index, int) or not 0 <= index < len(sessions):
        raise PilotGateError("资源调用缺少实际计费预约区间", 5)
    session = sessions[index]
    if (
        session.get("stage_execution_id") != execution["stage_execution_id"]
        or not session["start"]
        <= call["started_at"]
        <= call["stopped_at"]
        <= session["stop"]
    ):
        raise PilotGateError("资源调用不在其已结清预约区间内", 5)


def _validate_cost_protocol(protocol, contract):
    from .ara_pilot import PilotGateError, _verify_source_roles
    from .ara_research_schema import digest
    from .research_protocol import _validate_new_protocol

    if (
        digest(
            {
                key: value
                for key, value in protocol.items()
                if key != "protocol_hash"
            }
        )
        != protocol["protocol_hash"]
    ):
        raise PilotGateError("资源来源协议摘要不匹配")
    if (
        protocol.get("pilot") is not True
        or protocol.get("pilot_profile") != "full-calibration"
        or protocol.get("target_execution_hash") != digest(contract)
    ):
        raise PilotGateError("资源测量没有使用本目标完整 R2 校准")
    _validate_new_protocol(protocol)
    _verify_source_roles(
        protocol, contract["role_mappings"]["full-calibration"]
    )


def _validate_cost_operation(observation, execution, call):
    from .ara_pilot import PilotGateError

    phase = observation["phase"]
    if phase in {"shortlist", "export"}:
        valid = (
            execution["purpose"] == "resource"
            and observation["source"] == call["evidence_files"].get("resource")
            and observation["record_path"][:1] == ["measurements"]
            and len(observation["record_path"]) == 2
        )
    else:
        operation = "trial" if phase in {"load", "pilot"} else phase
        record = (
            "load"
            if phase == "load"
            else "reload"
            if phase == "reload"
            else "trial"
        )
        valid = (
            execution["operation"] == operation
            and observation["source"] == call["evidence_files"]["trial"]
            and observation["record_path"] == ["measurements", record]
        )
    if not valid:
        raise PilotGateError("计费阶段不是该原生调用的完整操作", 5)
