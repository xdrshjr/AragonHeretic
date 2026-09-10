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
    artifact_schema: Literal["cara-research-acceptance-v3"] = (
        "cara-research-acceptance-v3"
    )
    required_level: Literal[
        "recovery", "sample_extreme", "statistical_extreme"
    ] = "statistical_extreme"
    parent_candidate_lock_hash: str | None = None
    parent_candidate_lock_path: str | None = None
    wallclock_seconds: Literal[28800] = 28800
    max_cpu_rss_gib: float = Field(default=64.0, gt=0, le=64)

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
