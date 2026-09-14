# SPDX-License-Identifier: AGPL-3.0-or-later
"""从真实 pilot 和缓存数据准备独立的 Pro 6000 全量实验。"""

from __future__ import annotations

import importlib.metadata
import math
import subprocess
from pathlib import Path

from .ara_research_schema import digest, file_digest, read_json, write_json
from .research_protocol import (
    build_research_manifest,
    normalized_text_hash,
)

SCOPE = "pro6000-experiment-v1"
ROLE_SIZES = {
    "fit": 192,
    "monitor": 64,
    "mechanism-development": 44,
    "development": 100,
}
RUNTIME_CONFIG = "config.pro6000-experiment.toml"
DERIVED_PILOT_FIELDS = {
    "protocol_hash",
    "schema_version",
    "created_at",
    "roles",
    "methods",
    "search_budget",
    "preparation_access_log",
    "preparation_access_log_hash",
    "audit_sample_shortfall",
    "machine_migration",
    "numerical_retry",
}


def check_hardware() -> dict:
    """在分配模型前核对实际单卡和显存，输出可复查的硬件记录。"""
    output = (
        subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=20,
        )
        .strip()
        .splitlines()
    )
    if len(output) != 1:
        raise ValueError("该入口要求一张可见的 Pro 6000 GPU")
    name, total, used = [part.strip() for part in output[0].split(",")]
    if "PRO 6000" not in name.upper() or int(total) < 90 * 1024:
        raise ValueError(f"GPU 不满足 Pro 6000 96 GiB 配置：{output[0]}")
    if int(used) > 2048:
        raise ValueError(f"GPU 已被其他任务占用：{used} MiB")
    return {"name": name, "total_mib": int(total), "used_mib": int(used)}


def read_pilot(record: Path) -> tuple[dict, dict, Path]:
    """只复用已完成 pilot 的真实数据身份，不生成虚构预算证明。"""
    summary = read_json(record / "pilot-summary.json")
    if summary.get("status") != "passed" or summary.get("exit_code") != 0:
        raise ValueError("需要已完成且退出码为 0 的真实 pilot")
    if not summary.get("snapshot", {}).get("archive_hash_verified"):
        raise ValueError("pilot 缺少快照归档校验")
    folder = record / summary["attempt"]
    protocol = read_json(folder / "protocol.json")
    declared = protocol.pop("protocol_hash")
    if digest(protocol) != declared or declared != summary["protocol_hash"]:
        raise ValueError("pilot 协议摘要不匹配")
    protocol["protocol_hash"] = declared
    if protocol.get("pilot") is not True:
        raise ValueError("所选来源不是工程 pilot")
    return summary, protocol, folder


def split_full_rows(candidates: list, previous: dict) -> dict:
    """保留原角色题目，用未分配的真实题目扩充 fit/monitor。"""
    by_index = {row["row_index"]: row for row in candidates}
    selected, used = {}, set()
    for role, count in ROLE_SIZES.items():
        rows = previous[role]
        if len(rows) > count:
            raise ValueError(f"{role} 原始题目数超过全量上限")
        selected[role] = []
        for original in rows:
            actual = by_index.get(original["row_index"])
            if actual is None or any(
                actual[key] != original[key]
                for key in ("text", "source", "revision", "split")
            ):
                raise ValueError(f"{role} 缓存正文与 pilot 不一致")
            key = normalized_text_hash(actual["text"])
            if key in used:
                raise ValueError("pilot 角色之间存在重复题目")
            used.add(key)
            selected[role].append({**actual, **original})
    for role, count in ROLE_SIZES.items():
        for row in candidates:
            if len(selected[role]) == count:
                break
            key = normalized_text_hash(row["text"])
            if key not in used:
                selected[role].append(row)
                used.add(key)
        if len(selected[role]) != count:
            raise ValueError(f"{role} 去重后的真实数据不足 {count} 条")
    return selected


def _cached_rows(source, side):
    import pyarrow.parquet as parquet

    path = Path(source["source_cache_path"])
    if file_digest(path) != source["sha256"]:
        raise ValueError(f"{side} 缓存数据摘要已变化：{path}")
    rows = parquet.read_table(path, columns=["text"]).to_pylist()
    result = []
    for index, row in enumerate(rows):
        key = normalized_text_hash(row["text"])
        result.append(
            {
                "prompt_id": f"{side}-train-{index}",
                "scenario_group_id": f"text-{key}",
                "primary_in_group": True,
                "source": source["dataset"],
                "revision": source["revision"],
                "row_index": index,
                "split": "train",
                "language": "en",
                "category": f"source-{side}",
                "system": "You are a helpful assistant.",
                "text": row["text"],
            }
        )
    return result


def _previous_rows(pilot, folder, side):
    from .research_protocol import load_role_body

    previous = {}
    for role in ROLE_SIZES:
        name = f"{role}.{side}"
        bodies = load_role_body(pilot, folder, name)
        metadata = pilot["roles"][name]["prompts"]
        previous[role] = [
            {**identity, **body}
            for identity, body in zip(metadata, bodies, strict=True)
        ]
    return previous


def prepare_roles(root, pilot, folder):
    """冻结 400 条每侧的互斥角色；development 不随搜索结果选题。"""
    sources, provenance = {}, {}
    for side in ("good", "bad"):
        cached = pilot["roles"][f"fit.{side}"]["source"]["cached_source"]
        candidates = _cached_rows(cached, side)
        previous = _previous_rows(pilot, folder, side)
        selection = split_full_rows(candidates, previous)
        provenance[side] = cached
        for role, rows in selection.items():
            name = f"{role}.{side}"
            path = root / "inputs" / f"{name}.json"
            write_json(path, {"rows": rows}, immutable=True)
            sources[name] = {
                "path": path.relative_to(root).as_posix(),
                "sha256": file_digest(path),
                "license": cached["license"],
                "cached_source": cached,
            }
    write_json(root / "dataset-provenance.json", provenance, immutable=True)
    return sources


def prepare_config(root, run):
    import tomllib

    import tomli_w

    source = root / "source"
    template = source / "config.qwen38-27b-cara-v3-96.toml"
    settings = tomllib.loads(template.read_text(encoding="utf-8"))
    settings.update(
        model=run["model"],
        device_map="cuda:0",
        max_memory={"0": "88GiB", "cpu": "64GiB"},
        study_checkpoint_dir=str(root / "search"),
        seed=run["seed"],
    )
    settings["ara_v3"]["protocol_manifest"] = str(
        root / "protocol" / "protocol.json"
    )
    settings["ara_runtime_guard"].update(
        required_target_devices=["cuda:0"],
        max_cuda_allocated_gib=88.0,
        max_capture_cpu_gib=64.0,
    )
    if run.get("artifact_version") == "v3.1":
        settings["ara_v3"].update(
            artifact_schema="cara-research-acceptance-v3.1",
            proposal_policy="spectral-backtrack-v1",
            backtracking_alphas=[1, 0.5, 0.25, 0.125, 0.0625],
            pilot_profile="full-calibration",
        )
    else:
        for key in (
            "proposal_policy",
            "backtracking_alphas",
            "spectral_projection_margin",
            "pilot_profile",
            "stage_execution_id",
        ):
            settings["ara_v3"].pop(key, None)
        settings["ara_v3"]["artifact_schema"] = "cara-research-acceptance-v3"
    target = source / RUNTIME_CONFIG
    target.write_text(tomli_w.dumps(settings), encoding="utf-8", newline="\n")
    return settings


def source_identity(source):
    """绑定归档源码及本次运行 TOML，允许主工作区继续修改代码。"""
    paths = list((source / "src" / "heretic").rglob("*.py"))
    paths += list((source / "scripts").glob("*.py"))
    paths += list((source / "scripts").glob("*.sh"))
    paths += [source / RUNTIME_CONFIG, source / "pyproject.toml"]
    return {
        path.relative_to(source).as_posix(): file_digest(path)
        for path in sorted(paths)
    }


def _preparation_payload(root, run, pilot):
    payload = {
        key: value
        for key, value in pilot.items()
        if key not in DERIVED_PILOT_FIELDS
    }
    sources = source_identity(root / "source")
    payload.update(
        protocol_id=root.name,
        pilot=False,
        execution_scope=SCOPE,
        source_revision=run["source_revision"],
        source_files=sources,
        source_tree_hash=digest(sources),
        input_root=str(root),
        output=str(root / "protocol" / "protocol.json"),
        phase_budgets={
            "experiment": {
                "max_wallclock_seconds": run["hours"] * 3600,
                "devices": 1,
                "members": [f"S2-{run['seed']}"],
            }
        },
        parent_pilot_hash=pilot["protocol_hash"],
        research_limitations=[
            "全量 S2 参数实验；没有独立审计或语义双评",
            "不生成研究合格候选或论文验收通过结论",
        ],
    )
    payload["package_versions"] = {
        name: importlib.metadata.version(name)
        for name in pilot["package_versions"]
    }
    payload["sampling"] = {
        **pilot["sampling"],
        "seed": run["seed"],
        "population": "固定缓存训练集，每侧 400 条互斥角色题目",
        "inclusion_rules": "保留 pilot 各角色题目，按原始行号扩充 fit/monitor",
        "grouping_rule": "规范化文本分组，仅作 benchmark 实验",
    }
    if run.get("artifact_version") == "v3.1":
        _bind_new_experiment(payload, run)
    return payload


def _bind_new_experiment(payload, run):
    from .ara_pilot import read_bound
    from .ara_research_schema import validate_target_contract

    contract = read_bound(run["target_execution_contract"])
    validate_target_contract(contract)
    expected = {
        "quantization": contract["quantization"],
        "dtype": contract["dtype"],
        "judge_identity": contract["scorer_identity"],
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("单卡执行精度或评分器与目标契约不一致")
    payload.update(
        schema_version="cara-research-protocol-v3.1",
        target_execution_contract=contract,
        target_execution_hash=digest(contract),
        initialization_sources=contract["initialization_sources"],
        initialization_scheme="paired-data-v3.1",
        role_layout=contract["role_layout"],
        pilot_profile="full-calibration",
        replay_weight_comparison="effective-update-v1",
        source_files=contract["source_files"],
        source_tree_hash=digest(contract["source_files"]),
    )
    payload["phase_budgets"]["experiment"].update(
        max_gpu_hours=run["hours"],
        readiness_evidence=run["readiness_evidence"],
        members=[f"S2/spectral-backtrack-v1/{run['seed']}"],
    )


def snapshot_requirement(settings, remaining=24):
    """按实际目标 shape 预留剩余 trial、适配器导出和 20% 余量。"""
    total = sum(
        row["count"] * (row["in_features"] + row["out_features"])
        for row in settings["ara_runtime_guard"]["targets"]
    )
    return math.ceil(total * 128 * 4 * (remaining + 1) * 1.2)


def prepare_experiment(root: Path, run: dict) -> dict:
    """生成可独立恢复的冻结协议，尚不加载 GPU 模型。"""
    import shutil

    if run.get("artifact_version") == "v3.1":
        from .ara_pilot import read_bound, validate_readiness

        validate_readiness(
            read_bound(run["readiness_evidence"]),
            {
                "required_stage": "search_readiness",
                "target_execution_contract": read_bound(
                    run["target_execution_contract"]
                ),
            },
        )

    hardware = check_hardware()
    summary, pilot, folder = read_pilot(Path(run["pilot_record"]))
    settings = prepare_config(root, run)
    required = snapshot_requirement(settings)
    available = shutil.disk_usage(root).free
    if available < required:
        raise ValueError(f"共享盘不足：需要 {required}，可用 {available} 字节")
    sources = prepare_roles(root, pilot, folder)
    payload = _preparation_payload(root, run, pilot)
    payload["roles"] = sources
    payload["sampling"]["sampling_frame_hash"] = digest(sources)
    write_json(root / "preparation.json", payload, immutable=True)
    path = root / "protocol" / "protocol.json"
    # 中断发生在协议落盘与预检之间时复用原协议，避免刷新 created_at。
    protocol = (
        read_json(path) if path.exists() else build_research_manifest(payload)
    )
    return {
        "scope": SCOPE,
        "protocol_hash": protocol["protocol_hash"],
        "source_revision": run["source_revision"],
        "hardware": hardware,
        "disk_required_bytes": required,
        "disk_available_bytes": available,
        "sample_counts": {
            name: row["selected_count"]
            for name, row in protocol["roles"].items()
        },
        "pilot_wallclock_seconds": summary["pilot_budget_wallclock_seconds"],
        "experiment_hours_limit": run["hours"],
        "research_acceptance": "not_run",
    }
