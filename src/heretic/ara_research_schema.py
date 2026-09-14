# SPDX-License-Identifier: AGPL-3.0-or-later
"""研究制品的严格版本边界、不可变 JSON 和本地文件身份。"""

from __future__ import annotations

import hashlib
import json
import math
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .ara_refinement_config import PARAMETER_RANGES

PROTOCOL_SCHEMA = "cara-research-protocol-v3"
STUDY_SCHEMA = "cara-research-study-v3"
ACCEPTANCE_SCHEMA = "cara-research-acceptance-v3"
REPRODUCE_SCHEMA = "cara-research-reproduce-v3"
PROTOCOL_SCHEMA_V31 = "cara-research-protocol-v3.1"
STUDY_SCHEMA_V31 = "cara-research-study-v3.1"
ACCEPTANCE_SCHEMA_V31 = "cara-research-acceptance-v3.1"
REPRODUCE_SCHEMA_V31 = "cara-research-reproduce-v3.1"


class StrictRecord(BaseModel):
    """所有证据模型拒绝额外字段及非有限 JSON 数字。"""

    model_config = {"extra": "forbid", "allow_inf_nan": False}


class RefinementParameters(StrictRecord):
    """六维研究参数，与原生 v1/v2 包络明确分开。"""

    layer_start: float
    layer_span: float
    attn_strength: float
    mlp_strength: float
    push_weight: float
    margin: float

    @model_validator(mode="after")
    def validate_bounds(self) -> "RefinementParameters":
        """拒绝越界及非有限的参数。"""
        for name, (lower, upper) in PARAMETER_RANGES.items():
            if not lower <= getattr(self, name) <= upper:
                raise ValueError(f"研究参数 {name} 超出固定边界")
        return self

    def envelope(self) -> dict:
        """生成带明确目标版本的参数包络。"""
        return {
            "method": "ara",
            "objective_version": "sequential-v3",
            "schema": "cara-research-parameters-v3",
            "payload": self.model_dump(),
        }


class ScoreIdentity(StrictRecord):
    """评分的分母、参考轨迹、侧别及权重来源身份。"""

    scorer_version: str = Field(min_length=1)
    role: str = Field(min_length=1)
    prompt_ids: list[str] = Field(min_length=1)
    generation_profile_hash: str = Field(min_length=1)
    reference_sequence_hash: str | None
    base_identity: str = Field(min_length=1)
    candidate_identity: str = Field(min_length=1)
    prompt_count: int = Field(gt=0)
    token_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> "ScoreIdentity":
        """禁止重复题目或用截短的响应列表维持完整分母。"""
        if len(set(self.prompt_ids)) != self.prompt_count:
            raise ValueError("评分题目重复或分母错误")
        if len(self.prompt_ids) != self.prompt_count:
            raise ValueError("评分题目数不匹配")
        return self


class CandidateLock(StrictRecord):
    """候选锁定之后，重放失败不能触发重新选择。"""

    schema_version: Literal["cara-research-candidate-v3"] = (
        "cara-research-candidate-v3"
    )
    protocol_hash: str
    study_hash: str
    method_id: str
    seed: int
    attempt: int = Field(ge=0, lt=24)
    parameters: RefinementParameters
    shortlist: list[int]
    selection_rule_hash: str
    final_snapshot_path: str
    final_snapshot_hash: str
    score_evidence_hash: str
    eligibility: Literal["qualified", "comparison_only"]
    parent_candidate_lock_hash: str | None = None


class StudyExecutionIdentity(StrictRecord):
    """搜索开始前冻结的身份，不包含单次参数和后代结果。"""

    schema_version: Literal["cara-research-study-execution-v3.1"] = (
        "cara-research-study-execution-v3.1"
    )
    protocol_hash: str
    method_id: str
    proposal_policy: str
    seed: int
    execution_config: dict


class TrialExecutionIdentity(StrictRecord):
    """参数确定后绑定父 study 或固定 pilot 执行配置。"""

    schema_version: Literal["cara-research-execution-v3.1"] = (
        "cara-research-execution-v3.1"
    )
    study_execution_hash: str | None
    pilot_execution_config_hash: str | None
    attempt: int = Field(ge=0, lt=24)
    parameters: RefinementParameters
    parameter_hash: str
    method_id: str
    proposal_policy: str
    execution_config: dict

    @model_validator(mode="after")
    def validate_parent(self):
        if bool(self.study_execution_hash) == bool(
            self.pilot_execution_config_hash
        ):
            raise ValueError("trial 必须唯一绑定 study 或 pilot 父身份")
        if digest(self.parameters.model_dump()) != self.parameter_hash:
            raise ValueError("trial 参数摘要不匹配")
        return self


class StageExecution(StrictRecord):
    """阶段工作唯一主键；固定参数对照和重放分别注册。"""

    campaign_id: str
    stage: Literal["R1", "R2", "R3"]
    stage_execution_id: str = Field(min_length=1)
    member_id: str
    profile: Literal["smoke", "full-calibration", "search"]
    purpose: Literal["progress", "comparison", "resource", "replay"]
    parameters: RefinementParameters
    parameter_hash: str
    initialization_attempt: int = Field(ge=0, lt=24)
    operation: Literal["trial", "replay-1", "replay-2", "third-apply", "reload"]
    parent_stage_execution_id: str | None = None
    budget_key: str

    @model_validator(mode="after")
    def validate_execution(self):
        if digest(self.parameters.model_dump()) != self.parameter_hash:
            raise ValueError("阶段固定参数 hash 不匹配")
        if self.operation != "trial" and not self.parent_stage_execution_id:
            raise ValueError("重放和重载必须绑定父执行项")
        method, policy, seed = self.member_id.split("/")
        member_identity(method, policy, int(seed))
        return self


def _validate_target_fields(contract):
    required = {
        "schema_version",
        "campaign_id",
        "model_identity",
        "tokenizer_identity",
        "initialization_sources",
        "source_files",
        "package_versions",
        "members",
        "seeds",
        "parameter_ranges",
        "initialization_scheme",
        "role_layout",
        "role_mappings",
        "role_identities",
        "generation_profiles",
        "scorer_identity",
        "quantization",
        "dtype",
        "hardware",
        "stage_executions",
        "phase_budgets",
        "validation_rules",
    }
    if set(contract) != required:
        raise ValueError("目标执行契约字段不完整或包含未注册后代字段")
    if contract["schema_version"] != "cara-target-execution-v1":
        raise ValueError("目标执行契约 schema 不匹配")
    if contract["initialization_scheme"] != "paired-data-v3.1":
        raise ValueError("目标初始化规则不匹配")
    if contract["seeds"] != [42, 43, 44]:
        raise ValueError("目标契约必须固定三个 seed")


def validate_target_contract(contract):
    """目标契约不包含后代结果；完整内容参与摘要，未知字段拒绝。"""
    _validate_target_fields(contract)
    executions = [
        StageExecution.model_validate(row)
        for row in contract["stage_executions"]
    ]
    keys = [row.stage_execution_id for row in executions]
    if len(keys) != len(set(keys)) or not keys:
        raise ValueError("阶段执行 ID 缺失或重复")
    for row in executions:
        if row.campaign_id != contract["campaign_id"]:
            raise ValueError("阶段工作属于其他 campaign")
        if row.member_id not in contract["members"]:
            raise ValueError("阶段成员不在目标白名单")
        if (
            row.parent_stage_execution_id
            and row.parent_stage_execution_id not in keys
        ):
            raise ValueError("阶段父执行项未注册")
    from .ara_refinement_config import validate_target_settings
    from .research_protocol import validate_target_protocol_fields

    validate_target_settings(contract, executions)
    validate_target_protocol_fields(contract)
    _validate_stage_registry(executions)
    canonical_bytes(contract)
    return digest(contract)


def _validate_stage_registry(executions):
    from .ara_refinement_config import ANCHORS

    registry = {row.stage_execution_id: row for row in executions}
    required = {
        "r1-reject-anchor0": "reject-v1",
        "r1-scale-anchor0": "scale-v1",
        "r1-clip-anchor0": "spectral-clip-v1",
        "r1-backtrack-anchor0": "spectral-backtrack-v1",
        "r1-reject-quarter-anchor0": "reject-v1",
        "r2-backtrack-anchor0": "spectral-backtrack-v1",
        "r2-backtrack-anchor2-resource": "spectral-backtrack-v1",
    }
    if not required.keys() <= registry.keys():
        raise ValueError("阶段契约缺少 R1 五对照或 R2 两个固定执行项")
    for key, policy in required.items():
        row = registry[key]
        anchor = 2 if key.endswith("resource") else 0
        values = dict(zip(PARAMETER_RANGES, ANCHORS[anchor]))
        if "quarter" in key:
            for name in ("attn_strength", "mlp_strength"):
                values[name] *= 0.25
        if (
            row.member_id != f"S2/{policy}/42"
            or row.parameters.model_dump() != values
        ):
            raise ValueError("阶段固定参数或方法策略与预注册不一致")
        purpose = (
            "resource"
            if key.endswith("resource")
            else "progress"
            if "backtrack-anchor0" in key
            else "comparison"
        )
        if row.purpose != purpose or row.operation != "trial":
            raise ValueError("阶段固定调用目的或操作不匹配")
    _validate_parent_executions(executions, registry)


def _validate_parent_executions(executions, registry):
    for row in executions:
        if row.parent_stage_execution_id:
            parent = registry[row.parent_stage_execution_id]
            for key in (
                "parameters",
                "initialization_attempt",
                "member_id",
                "stage",
            ):
                if getattr(row, key) != getattr(parent, key):
                    raise ValueError("重放调用必须继承父执行参数、初态和身份")
    for parent, required_ops in (
        ("r1-backtrack-anchor0", {"reload"}),
        (
            "r2-backtrack-anchor0",
            {"replay-1", "replay-2", "third-apply", "reload"},
        ),
    ):
        operations = [
            row.operation
            for row in executions
            if row.parent_stage_execution_id == parent
        ]
        if not required_ops.issubset(operations) or len(operations) != len(
            set(operations)
        ):
            raise ValueError("试点重放/重载调用清单缺失或重复")


class CandidateLockV31(CandidateLock):
    """新候选显式绑定逻辑执行身份；不改变旧锁的严格读取。"""

    schema_version: Literal["cara-research-candidate-v3.1"] = (
        "cara-research-candidate-v3.1"
    )
    proposal_policy: str
    study_execution_hash: str = Field(min_length=1)
    execution_identity_hash: str = Field(min_length=1)


def parse_candidate_lock(value):
    """按精确版本读取正式候选，拒绝 pilot 锁和未知版本。"""
    schemas = {
        "cara-research-candidate-v3": CandidateLock,
        "cara-research-candidate-v3.1": CandidateLockV31,
    }
    version = value.get("schema_version", "cara-research-candidate-v3")
    if version not in schemas:
        raise ValueError("候选锁 schema 未注册")
    return schemas[version].model_validate(value)


def research_version(value, kind):
    """返回精确 schema 代次；禁止按前缀接受未来未知版本。"""
    schema = value.get("schema_version", value.get("schema"))
    versions = {
        f"cara-research-{kind}-v3": "v3",
        f"cara-research-{kind}-v3.1": "v3.1",
    }
    if schema not in versions:
        raise ValueError(f"研究 {kind} schema 未注册：{schema}")
    return versions[schema]


def member_identity(method, policy, seed):
    """正式成员显示 ID 与文件存储键分别计算。"""
    from .ara_refinement_config import METHODS

    if method not in METHODS or seed not in (42, 43, 44):
        raise ValueError("研究成员方法或 seed 未注册")
    if policy not in {
        "reject-v1",
        "scale-v1",
        "spectral-clip-v1",
        "spectral-backtrack-v1",
    }:
        raise ValueError("研究成员策略未注册")
    if method in {"B1", "B2"} and policy != "reject-v1":
        raise ValueError("旧求解器成员策略不合法")
    member_id = f"{method}/{policy}/{seed}"
    return {
        "member_id": member_id,
        "member_storage_key": digest(member_id),
        "method_id": method,
        "proposal_policy": policy,
        "seed": seed,
    }


def build_execution_identity(protocol, config, seed, trial=None):
    """固定父身份后构造 trial；不将实际参数反写 study。"""
    execution = {
        "refinement": config.model_dump(mode="json"),
        "initialization_scheme": "paired-data-v3.1",
        "replay_weight_comparison": "effective-update-v1",
    }
    parent = StudyExecutionIdentity(
        protocol_hash=protocol["protocol_hash"],
        method_id=config.method_id,
        proposal_policy=config.proposal_policy,
        seed=seed,
        execution_config=execution,
    ).model_dump(mode="json")
    if trial is None:
        return parent
    parameters, attempt = trial
    pilot = bool(protocol.get("pilot"))
    pilot_config = {
        "source_protocol_hash": protocol["protocol_hash"],
        "execution": parent,
        "stage_execution_id": config.stage_execution_id,
    }
    return TrialExecutionIdentity(
        study_execution_hash=None if pilot else digest(parent),
        pilot_execution_config_hash=digest(pilot_config) if pilot else None,
        attempt=attempt,
        parameters=parameters,
        parameter_hash=digest(parameters.model_dump()),
        method_id=config.method_id,
        proposal_policy=config.proposal_policy,
        execution_config=execution,
    ).model_dump(mode="json")


def canonical_bytes(value: Any) -> bytes:
    """编码规范 JSON，拒绝 NaN/Infinity。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest(value: Any) -> str:
    """返回规范 JSON 的 SHA-256。"""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_digest(path: str | Path) -> str:
    """分块计算文件摘要。"""
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def resolve_artifact(root: str | Path, relative: str) -> Path:
    """拒绝绝对路径、父目录穿越及指向根目录外的符号链接。"""
    base = Path(root).resolve()
    candidate = (base / relative).resolve()
    if Path(relative).is_absolute() or "\\" in relative:
        raise ValueError("制品路径必须是使用 / 的相对路径")
    if not candidate.is_relative_to(base) or candidate == base:
        raise ValueError("制品路径超出运行目录")
    return candidate


def read_json(path: str | Path) -> dict:
    """读取 JSON 对象并拒绝非有限值。"""
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} 必须包含 JSON 对象")
    canonical_bytes(value)
    return value


def write_json(path: str | Path, value: dict, immutable=False) -> None:
    """以 fsync 和原子替换落盘；冻结文件仅允许幂等重复。"""
    path = Path(path)
    encoded = canonical_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        if path.read_bytes() != encoded:
            raise ValueError(f"不可变制品已存在且内容不同：{path.name}")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if immutable:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def exclusive_lock(path: str | Path):
    """跨平台进程锁；进程退出自动释放，锁文件可保留。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError(f"运行目录已被其他进程锁定：{path}") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def validate_research_parameters(envelope: dict) -> RefinementParameters:
    """解析独立 v3 包络，禁止误读旧参数。"""
    expected = {"method", "objective_version", "schema", "payload"}
    if set(envelope) != expected:
        raise ValueError("v3 参数包络字段不匹配")
    if envelope["method"] != "ara":
        raise ValueError("v3 方法必须为 ARA")
    if envelope["objective_version"] != "sequential-v3":
        raise ValueError("v3 目标版本不匹配")
    if envelope["schema"] != "cara-research-parameters-v3":
        raise ValueError("v3 参数 schema 不匹配")
    return RefinementParameters.model_validate(envelope["payload"])


def validate_research_report(report: dict) -> None:
    """验证研究报告状态及正式通过的必要证据。"""
    canonical_bytes(report)
    version = research_version(report, "acceptance")
    if version == "v3.1" and report.get("research_claim") not in {
        "statistical_supported",
        "sample_only",
        "recovery_supported",
        "insufficient_samples",
        "assumption_unmet",
        "not_supported",
        "pending",
    }:
        raise ValueError("新版研究声明枚举非法")
    if report.get("status") not in {"passed", "failed", "inconclusive"}:
        raise ValueError("研究验收状态不合法")
    levels = ("recovery", "sample_extreme", "statistical_extreme")
    if report.get("required_level") not in levels:
        raise ValueError("研究目标层不合法")
    if report["status"] != "passed":
        return
    required = ["engineering_status", "effect_status", "ability_status"]
    if report["required_level"] != "recovery":
        required.append("sample_extreme_status")
    if report["required_level"] == "statistical_extreme":
        required.append("statistical_extreme_status")
    if any(report.get(key) != "passed" for key in required):
        raise ValueError("passed 报告缺少必要通过状态")
    for name in ("selected_candidate", "audit_plan_hash", "core_hashes"):
        if not report.get(name):
            raise ValueError(f"passed 报告缺少 {name}")
    _validate_passed_evidence(report)


def _validate_passed_evidence(report):
    lock = parse_candidate_lock(report["selected_candidate"])
    if research_version(lock.model_dump(), "candidate") != research_version(
        report, "acceptance"
    ):
        raise ValueError("验收与候选锁版本不能混用")
    metrics = report.get("metrics", {})
    for side in ("good", "bad"):
        row = metrics.get(side, {})
        n, k = row.get("n", 0), row.get("k", -1)
        if not isinstance(n, int) or not isinstance(k, int) or not 0 <= k <= n:
            raise ValueError("passed 报告的语义计数无效")
        if n == 0 or row.get("status") != "complete" or row.get("missing"):
            raise ValueError("passed 报告缺少完整双侧语义标签")
        if row.get("refusal_rate") != k / n:
            raise ValueError("passed 报告语义比率与分母不一致")
    ability = report.get("ability", {})
    if ability.get("status") != "passed":
        raise ValueError("passed 报告缺少能力区间证据")


def validate_research_reproduce(value: dict) -> None:
    """拒绝没有完整协议及验收绑定的 v3 复现文件。"""
    version = research_version(value, "reproduce")
    expected = {
        "schema",
        "protocol_hash",
        "candidate_lock_hash",
        "parameters",
        "acceptance_sha256",
        "core_hashes",
    }
    if version == "v3.1":
        expected |= {"execution_identity_hash", "study_execution_hash"}
    if set(value) != expected:
        raise ValueError("研究复现字段不符合严格 schema")
    canonical_bytes(value)
    validate_research_parameters(value["parameters"])
    required = (
        "protocol_hash",
        "candidate_lock_hash",
        "acceptance_sha256",
        "core_hashes",
    )
    if version == "v3.1":
        required += ("execution_identity_hash", "study_execution_hash")
    for name in required:
        if not value.get(name):
            raise ValueError(f"研究复现缺少 {name}")


def require_finite(values) -> None:
    """在数值证据边界拒绝非有限值。"""
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError("研究指标包含非有限值")


def _ara_payload(parameters) -> dict:
    from dataclasses import asdict

    return {
        "start_layer_index": parameters.start_layer_index,
        "end_layer_index": parameters.end_layer_index,
        "components": {
            name: asdict(component)
            for name, component in parameters.components.items()
        },
    }


def serialize_method_parameters(parameters) -> dict:
    """Serialize resolved method parameters using the reproduce-v4 envelope."""

    from dataclasses import asdict
    from .ara import ARAParameters
    from .ara_search import (
        ARATrajectoryParameters,
        parameter_envelope as trajectory_parameter_envelope,
    )

    if isinstance(parameters, RefinementParameters):
        return parameters.envelope()
    if isinstance(parameters, ARAParameters):
        return {"method": "ara", "payload": _ara_payload(parameters)}
    if isinstance(parameters, ARATrajectoryParameters):
        return trajectory_parameter_envelope(parameters)
    return {
        "method": "directional",
        "payload": {
            "direction_index": parameters.direction_index,
            "abliteration_parameters": {
                name: asdict(component)
                for name, component in parameters.components.items()
            },
        },
    }


def parse_method_parameters(envelope):
    """Validate and deserialize a reproduce-v4 method envelope."""

    from collections.abc import Mapping
    from typing import cast
    from .ara import ARAParameters, ARAComponentParameters
    from .ara_search import (
        parse_parameter_envelope as parse_trajectory_parameter_envelope,
    )

    if envelope.get("objective_version") == "sequential-v3":
        return validate_research_parameters(dict(envelope))
    if envelope.get("objective_version") == "trajectory-v2":
        return parse_trajectory_parameter_envelope(envelope)
    method = envelope.get("method")
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("method parameter payload must be an object")
    if method == "directional":
        return _parse_directional_payload(payload)
    if method == "ara":
        raw_components = cast(
            Mapping[str, Mapping[str, Any]], payload.get("components")
        )
        if not isinstance(raw_components, Mapping):
            raise ValueError("ARA payload has no components")
        return ARAParameters(
            start_layer_index=int(payload["start_layer_index"]),
            end_layer_index=int(payload["end_layer_index"]),
            components={
                name: ARAComponentParameters(**value)
                for name, value in raw_components.items()
            },
        )
    raise ValueError(f"unsupported abliteration method: {method}")


def _parse_directional_payload(payload):
    from collections.abc import Mapping
    from typing import cast
    from .model import AbliterationParameters
    from .trial_methods import DirectionalParameters

    raw_components = cast(
        Mapping[str, Mapping[str, Any]], payload.get("abliteration_parameters")
    )
    if not isinstance(raw_components, Mapping):
        raise ValueError("directional payload has no abliteration_parameters")
    return DirectionalParameters(
        direction_index=cast(float | None, payload.get("direction_index")),
        components={
            name: AbliterationParameters(**value)
            for name, value in raw_components.items()
        },
    )


def study_identity(settings, protocol):
    if protocol.get("schema_version") == "cara-research-protocol-v3.1":
        identity = build_execution_identity(
            protocol, settings.ara_v3, settings.seed
        )
        return {
            "protocol_hash": protocol["protocol_hash"],
            "method_id": settings.ara_v3.method_id,
            "proposal_policy": settings.ara_v3.proposal_policy,
            "seed": settings.seed,
            "study_execution": identity,
            "study_execution_hash": digest(identity),
        }
    values = settings.model_dump(mode="json")
    # 新配置默认值不能进入旧 journal 的已冻结 settings_hash。
    for key in (
        "proposal_policy",
        "backtracking_alphas",
        "spectral_projection_margin",
        "pilot_profile",
        "stage_execution_id",
    ):
        values.get("ara_v3", {}).pop(key, None)
    return {
        "protocol_hash": protocol["protocol_hash"],
        "method_id": settings.ara_v3.method_id,
        "seed": settings.seed,
        "settings_hash": digest(values),
    }
