# SPDX-License-Identifier: AGPL-3.0-or-later
"""独立准备正文，训练只读取冻结身份及开发角色。"""

from __future__ import annotations

import hashlib
import random
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from .ara_refinement_config import METHODS, ROLE_COUNTS
from .ara_research_schema import (
    PROTOCOL_SCHEMA,
    digest,
    file_digest,
    read_json,
    resolve_artifact,
    write_json,
)

AUDIT_ROLES = {"legacy-audit", "research-audit", "ability-audit"}
REQUIRED_ROLES = {
    f"{role}.{side}"
    for role in ("fit", "monitor", "mechanism-development", "development")
    for side in ("good", "bad")
}


def normalized_text_hash(text: str) -> str:
    """规范化 Unicode 与空白，避免文本改写格式造成泄漏。"""
    normalized = " ".join(unicodedata.normalize("NFKC", text).split())
    if not normalized:
        raise ValueError("协议不能包含空问题")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_audit_role(role: str) -> bool:
    """识别必须与训练隔离的拒答及能力角色。"""
    return role.split(".")[0] in AUDIT_ROLES


def _check_configuration(config: dict) -> None:
    required = {
        "protocol_id",
        "source_tree_hash",
        "source_files",
        "package_versions",
        "model_identity",
        "tokenizer_identity",
        "quantization",
        "dtype",
        "seeds",
        "required_level",
        "generation_profiles",
        "sampling",
        "phase_budgets",
        "roles",
        "ability_tasks",
        "judge_identity",
        "audit_history",
        "output",
    }
    if required - config.keys():
        raise ValueError(
            f"协议准备缺少字段：{sorted(required - config.keys())}"
        )
    if config["seeds"] != [42, 43, 44]:
        raise ValueError("正式协议必须预注册三个独立训练 seed")
    if config["required_level"] not in {
        "recovery",
        "sample_extreme",
        "statistical_extreme",
    }:
        raise ValueError("研究目标层未知")
    if REQUIRED_ROLES - config["roles"].keys():
        raise ValueError("协议缺少开发角色或 good/bad 侧别")
    for key in ("model_identity", "tokenizer_identity"):
        identity = config[key]
        if not identity.get("revision") or not identity.get("file_hashes"):
            raise ValueError(f"{key} 必须固定 revision 和实际文件摘要")
    _validate_sampling(config["sampling"])
    _validate_generation_profiles(config["generation_profiles"])


def _validate_sampling(sampling):
    for key in (
        "sampling_frame_hash",
        "population",
        "inclusion_rules",
        "exclusion_rules",
        "grouping_rule",
        "seed",
        "inference_scope",
    ):
        if key not in sampling:
            raise ValueError(f"抽样协议缺少 {key}")
    if sampling["inference_scope"] not in {"iid_population", "benchmark_only"}:
        raise ValueError("抽样推断范围未知")


def _validate_generation_profiles(profiles):
    expected = {"fit": 8, "keywords": 100, "semantic": 256, "sequence_kl": 32}
    for name, count in expected.items():
        if profiles.get(name) != count:
            raise ValueError(f"生成协议必须固定 {name}={count}")
    if (
        profiles.get("chat_template_kwargs", {}).get("enable_thinking")
        is not False
    ):
        raise ValueError("生成协议必须固定关闭 thinking")
    kwargs = profiles.get("generation_kwargs", {})
    if kwargs.get("do_sample", False) or kwargs.get("num_beams", 1) != 1:
        raise ValueError("生成协议必须固定贪心解码")


def _prepare_rows(role: str, source: dict, root: Path) -> tuple:
    if not source.get("license"):
        raise ValueError(f"{role} 缺少已核对的来源许可")
    rows_path = resolve_artifact(root, source["path"])
    if file_digest(rows_path) != source["sha256"]:
        raise ValueError(f"源文件摘要不匹配：{role}")
    payload = read_json(rows_path)
    rows = payload["rows"]
    identities, bodies = [], []
    for row in rows:
        for key in (
            "prompt_id",
            "scenario_group_id",
            "primary_in_group",
            "source",
            "revision",
            "row_index",
            "language",
            "category",
            "text",
            "system",
        ):
            if key not in row:
                raise ValueError(f"{role} 问题缺少 {key}")
        if not row["revision"] or type(row["row_index"]) is not int:
            raise ValueError("问题必须绑定不可变版本与原始行号")
        identity = {
            key: value
            for key, value in row.items()
            if key not in {"text", "system"}
        }
        identity.update(
            role=role,
            normalized_text_hash=normalized_text_hash(row["text"]),
            historically_observed=not is_audit_role(role),
        )
        identities.append(identity)
        bodies.append(
            {
                "prompt_id": row["prompt_id"],
                "text": row["text"],
                "system": row["system"],
            }
        )
    return identities, {"rows": bodies}


def _validate_identity_isolation(roles: dict) -> None:
    seen_ids, seen_rows, seen_text, seen_groups = set(), {}, {}, {}
    for role, bundle in roles.items():
        for row in bundle["prompts"]:
            prompt_id = row["prompt_id"]
            if prompt_id in seen_ids:
                raise ValueError(f"prompt_id 重复：{prompt_id}")
            seen_ids.add(prompt_id)
            origin = (
                row["source"],
                row["revision"],
                row.get("split"),
                row["row_index"],
            )
            checks = (
                (seen_rows, origin),
                (seen_text, row["normalized_text_hash"]),
                (seen_groups, row["scenario_group_id"]),
            )
            for registry, key in checks:
                previous = registry.get(key)
                if previous is not None and previous != role:
                    raise ValueError(f"跨角色泄漏：{previous} 与 {role}")
                registry[key] = role


def _validate_role_count(role: str, rows: list, pilot: bool) -> None:
    name = role.split(".")[0]
    if name not in set(ROLE_COUNTS) | AUDIT_ROLES:
        raise ValueError(f"未注册的数据角色：{role}")
    if name != "ability-audit" and role.rsplit(".", 1)[-1] not in {
        "good",
        "bad",
    }:
        raise ValueError(f"数据角色缺少侧别：{role}")
    expected = ROLE_COUNTS.get(name)
    if pilot and name in {"fit", "monitor"}:
        expected = 8
    if expected is not None and len(rows) != expected:
        raise ValueError(f"{role} 需要 {expected} 条，实际 {len(rows)}")
    if name == "legacy-audit" and len(rows) != 100:
        raise ValueError("legacy-audit 必须包含 100 条")
    hashes = [row["normalized_text_hash"] for row in rows]
    if len(hashes) != len(set(hashes)):
        raise ValueError(f"{role} 存在重复内容；禁止静默删行")
    if is_audit_role(role):
        groups = {}
        for row in rows:
            group = row["scenario_group_id"]
            groups[group] = groups.get(group, 0) + int(row["primary_in_group"])
        if any(count != 1 for count in groups.values()):
            raise ValueError(f"{role} 每个情景组必须且只能有一个主问题")


def build_research_manifest(config: dict) -> dict:
    """独立准备入口：读取正文、去重并冻结身份清单及分角色正文。"""
    _check_configuration(config)
    output = Path(config["output"]).resolve()
    root = Path(config.get("input_root", ".")).resolve()
    roles, bodies, access = {}, {}, []
    pilot = bool(config.get("pilot", False))
    for role, source in config["roles"].items():
        rows, text = _prepare_rows(role, source, root)
        _validate_role_count(role, rows, pilot)
        relative = f"bodies/{role}.json"
        selected = 8 if pilot else 96
        roles[role] = {
            "prompts": rows,
            "candidate_count": len(rows),
            "selected_count": selected
            if role.startswith("fit.")
            else len(rows),
            "body_path": relative,
            "body_hash": digest(text),
            "source": source,
        }
        bodies[relative] = text
        access.append(
            {
                "role": role,
                "purpose": "independent_preparation",
                "source_hash": source["sha256"],
            }
        )
    _validate_identity_isolation(roles)
    _validate_audit_history(config, roles)
    protocol = _protocol_payload(config, roles, access)
    protocol["protocol_hash"] = digest(protocol)
    for relative, text in bodies.items():
        write_json(output.parent / relative, text, immutable=True)
    write_json(output, protocol, immutable=True)
    return protocol


def _validate_audit_history(config, roles):
    history = config["audit_history"]
    for role in roles:
        if not is_audit_role(role):
            continue
        record = history.get(role)
        if not isinstance(record, dict):
            raise ValueError(f"{role} 缺少明确审计历史")
        if record.get("training_accessed") is not False:
            raise ValueError(f"{role} 独立性无法证明")
        if record.get("evaluation_accessed") is not False:
            raise ValueError(f"{role} 已消费或消费历史未知")
        if not record.get("evidence_hash"):
            raise ValueError(f"{role} 缺少历史账本证据摘要")


def _protocol_payload(config, roles, access):
    excluded = {"output", "input_root", "roles"}
    payload = {
        key: value for key, value in config.items() if key not in excluded
    }
    payload.update(
        schema_version=PROTOCOL_SCHEMA,
        roles=roles,
        created_at=datetime.now(timezone.utc).isoformat(),
        methods={key: list(value) for key, value in METHODS.items()},
        search_budget={"anchors": 4, "random": 8, "tpe": 12},
        preparation_access_log=access,
        preparation_access_log_hash=digest(access),
    )
    missing = {}
    for side in ("good", "bad"):
        rows = roles.get(f"research-audit.{side}", {}).get("prompts", [])
        groups = {r["scenario_group_id"] for r in rows if r["primary_in_group"]}
        missing[side] = max(0, 300 - len(groups))
    payload["audit_sample_shortfall"] = missing
    return payload


def prepare_protocol(config, metadata_loader=None, *, experiment=False) -> dict:
    """模型加载前校验冻结身份和开发正文；从不打开审计正文。"""
    path = Path(config.ara_v3.protocol_manifest)
    if not path.is_file():
        raise ValueError("缺少冻结 protocol.json；请先完成独立数据准备")
    protocol = read_json(path)
    expected = protocol.pop("protocol_hash", None)
    if protocol.get("schema_version") != PROTOCOL_SCHEMA:
        raise ValueError("研究协议 schema 不匹配")
    if digest(protocol) != expected:
        raise ValueError("冻结协议 hash 不匹配")
    protocol["protocol_hash"] = expected
    scope = protocol.get("execution_scope")
    if scope and not experiment:
        raise ValueError("独立实验协议必须使用对应实验入口，禁止研究提升")
    if experiment and scope != "pro6000-experiment-v1":
        raise ValueError("Pro 6000 实验协议范围不匹配")
    if REQUIRED_ROLES - protocol["roles"].keys():
        raise ValueError("训练协议缺少必需的开发角色")
    if protocol["required_level"] != config.ara_v3.required_level:
        raise ValueError("冻结后不能改变 required_level")
    _validate_identity_isolation(protocol["roles"])
    for role, bundle in protocol["roles"].items():
        _validate_role_count(
            role, bundle["prompts"], protocol.get("pilot", False)
        )
        source = bundle["source"]
        _validate_bundle_counts(role, bundle, protocol.get("pilot", False))
        if "dataset_spec" in source:
            _preflight_source(source["dataset_spec"], metadata_loader)
            _validate_source_rows(bundle)
        if not is_audit_role(role):
            load_role_body(protocol, path.parent, role)
    _validate_runtime_identity(config, protocol)
    return protocol


def _validate_bundle_counts(role, bundle, pilot):
    count = len(bundle["prompts"])
    selected = (8 if pilot else 96) if role.startswith("fit.") else count
    if (
        bundle["candidate_count"] != count
        or bundle["selected_count"] != selected
    ):
        raise ValueError(f"{role} 候选池/选中数量与协议不一致")


def _validate_source_rows(bundle):
    from .protocol_data import parse_absolute_split

    specification = bundle["source"]["dataset_spec"]
    interval = parse_absolute_split(specification["split"])
    rows = bundle["prompts"]
    if [row["row_index"] for row in rows] != list(
        range(interval.start, interval.stop)
    ):
        raise ValueError("冻结问题原始行号与数据切分不一致")
    for row in rows:
        if (
            row["source"] != specification["dataset"]
            or row["revision"] != specification["commit"]
        ):
            raise ValueError("冻结问题 source/revision 与数据源不一致")
        if row.get("split") != interval.name:
            raise ValueError("冻结问题 split 与数据源不一致")


def _validate_runtime_identity(settings, protocol):
    from importlib.metadata import version

    packages = protocol.get("package_versions", {})
    required = {"torch", "transformers", "peft", "optuna", "lm_eval"}
    if not required.issubset(packages):
        raise ValueError("协议缺少关键运行依赖版本")
    for name, expected in packages.items():
        if version(name) != expected:
            raise ValueError(f"运行依赖版本与冻结协议不匹配：{name}")
    profiles = protocol["generation_profiles"]
    _validate_generation_profiles(profiles)
    if profiles["chat_template_kwargs"] != settings.chat_template_kwargs:
        raise ValueError("运行 chat template 参数与冻结协议不一致")
    if profiles.get("generation_kwargs", {}) != settings.generation_kwargs:
        raise ValueError("运行生成参数与冻结协议不一致")
    if settings.model_commit != protocol["model_identity"]["revision"]:
        raise ValueError("运行模型 revision 与冻结协议不匹配")
    if settings.quantization != protocol["quantization"]:
        raise ValueError("运行量化方式与冻结协议不匹配")
    if settings.dtypes != [protocol["dtype"]]:
        raise ValueError("研究禁止在运行时切换计算类型")
    _validate_file_identities(settings, protocol)


def _validate_file_identities(settings, protocol):
    source_root = Path(__file__).resolve().parents[2]
    files = protocol.get("source_files")
    if not files or digest(files) != protocol["source_tree_hash"]:
        raise ValueError("协议缺少可核验的实际源码清单")
    for relative, expected in files.items():
        if file_digest(resolve_artifact(source_root, relative)) != expected:
            raise ValueError(f"研究源码已改变：{relative}")
    model_root = Path(settings.model)
    if not model_root.is_dir():
        raise ValueError("研究配置必须指向已准备并核验的本地模型缓存")
    for identity in (
        protocol["model_identity"],
        protocol["tokenizer_identity"],
    ):
        for relative, expected in identity["file_hashes"].items():
            if file_digest(resolve_artifact(model_root, relative)) != expected:
                raise ValueError(f"模型/tokenizer 文件 hash 不匹配：{relative}")


def _preflight_source(specification, loader):
    from .ara_config import DatasetSpecification
    from .protocol_data import preflight_audit_metadata

    preflight_audit_metadata(
        DatasetSpecification.model_validate(specification), loader
    )


def load_role_body(protocol: dict, root: Path, role: str) -> list[dict]:
    """训练角色正文加载器；审计必须经过独立消费入口。"""
    if is_audit_role(role):
        raise ValueError("训练入口禁止读取审计正文")
    return _read_bound_body(protocol, root, role)


def _read_bound_body(protocol, root, role):
    bundle = protocol["roles"][role]
    contents = read_json(resolve_artifact(root, bundle["body_path"]))
    if digest(contents) != bundle["body_hash"]:
        raise ValueError(f"{role} 正文 hash 不匹配")
    rows = contents["rows"]
    identities = bundle["prompts"]
    if len(rows) != len(identities):
        raise ValueError(f"{role} 正文数量不匹配")
    for identity, row in zip(identities, rows, strict=True):
        if identity["prompt_id"] != row["prompt_id"]:
            raise ValueError(f"{role} 正文顺序不匹配")
        if (
            normalized_text_hash(row["text"])
            != identity["normalized_text_hash"]
        ):
            raise ValueError(f"{role} 规范化正文不匹配")
    return rows


def select_fit_rows(protocol: dict, side: str, seed: int) -> list[dict]:
    """从完整候选池按共同 seed 选题，保留原始行身份。"""
    bundle = protocol["roles"][f"fit.{side}"]
    generator = random.Random(int(digest([seed, "fit", side])[:16], 16))
    selected = generator.sample(
        range(bundle["candidate_count"]), bundle["selected_count"]
    )
    return [bundle["prompts"][index] for index in sorted(selected)]


def fit_selection(settings, protocol, protocol_root):
    """将已冻结的共同抽样身份解析为模型输入。"""
    from .utils import Prompt

    fit, identities = {}, {}
    for side in ("good", "bad"):
        selected = select_fit_rows(protocol, side, settings.seed)
        identities[side] = [row["prompt_id"] for row in selected]
        rows = load_role_body(protocol, protocol_root, f"fit.{side}")
        by_id = {
            row["prompt_id"]: Prompt(row["system"], row["text"]) for row in rows
        }
        fit[side] = [by_id[key] for key in identities[side]]
    return fit, identities
