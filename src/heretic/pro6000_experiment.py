# SPDX-License-Identifier: AGPL-3.0-or-later
"""可恢复的单卡全量参数实验，复用 v3 求解和 4+8+12 搜索。"""

from __future__ import annotations

import argparse
import signal
import sys
import tempfile
import time
from pathlib import Path

from .ara_research_schema import (
    digest,
    exclusive_lock,
    file_digest,
    read_json,
    write_json,
)
from .pro6000_prepare import (
    RUNTIME_CONFIG,
    SCOPE,
    check_hardware,
    prepare_experiment,
)


def read_run(root):
    """恢复时继续使用原来的源码、参数和预算身份。"""
    run = read_json(root / "run.json")
    declared = run.pop("run_hash")
    if digest(run) != declared or run.get("scope") != SCOPE:
        raise ValueError("运行清单已变化或不属于 Pro 6000 实验")
    if Path(run["run_dir"]).resolve() != root.resolve():
        raise ValueError("实验目录已移动，绝对快照路径不能静默改写")
    return run


def write_status(root, stage, **details):
    """状态文件保持简短，完整数值和响应存入独立 trial 记录。"""
    payload = {"scope": SCOPE, "stage": stage, "updated_at": time.time()}
    write_json(root / "status.json", {**payload, **details})
    print(f"Pro 6000 实验阶段：{stage}", flush=True)


def choose_experiment_candidate(context, study, path):
    """只选开发指标对照，不调用语义审计或产生合格研究候选。"""
    from .ara_research_acceptance import development_gate

    completed = [row for row in study["trials"] if row["state"] == "COMPLETE"]
    feasible = [row for row in completed if development_gate(row["scores"])]
    candidates = feasible or completed
    study["execution_scope"] = SCOPE
    study["research_status"] = "not_run"
    if candidates:
        selected = min(
            candidates,
            key=lambda row: (
                row["scores"]["keywords"],
                row["scores"]["first_token_kl"],
                row["attempt"],
            ),
        )
        study["experiment_candidate"] = {
            "attempt": selected["attempt"],
            "eligibility": "comparison_only",
            "numeric_gate_passed": bool(feasible),
            "snapshot_hash": selected["snapshot_file_hash"],
        }
    write_json(path, study)


def _export_adapter(context, selected, root):
    import torch

    from .ara_refinement_capture import apply_snapshot, snapshot_factors

    snapshot = Path(selected["final_snapshot_path"])
    if file_digest(snapshot) != selected["snapshot_file_hash"]:
        raise ValueError("选中快照损坏，拒绝导出")
    factors = torch.load(snapshot, map_location="cpu", weights_only=True)
    model = context["model"]
    original = snapshot_factors(model.ara_targets)
    export = root / "artifacts" / "best-adapter"
    export.mkdir(parents=True, exist_ok=True)
    try:
        apply_snapshot(
            model.ara_targets, factors, selected["final_snapshot_hash"]
        )
        model.model.save_pretrained(export, safe_serialization=True)
        model.tokenizer.save_pretrained(export)
    finally:
        apply_snapshot(model.ara_targets, original)
    files = {
        path.name: file_digest(path)
        for path in export.iterdir()
        if path.is_file()
    }
    if "adapter_config.json" not in files or not any(
        name.endswith(".safetensors") for name in files
    ):
        raise RuntimeError("适配器导出文件不完整")
    return {"path": str(export), "file_hashes": files}


def _finish_experiment(context, study, root):
    completed = [row for row in study["trials"] if row["state"] == "COMPLETE"]
    result = {
        "scope": SCOPE,
        "research_status": "not_run",
        "source_revision": context["protocol"]["source_revision"],
        "protocol_hash": context["protocol"]["protocol_hash"],
        "attempts": len(study["trials"]),
        "completed_trials": len(completed),
        "failed_trials": len(study["trials"]) - len(completed),
        "selection": study.get("experiment_candidate"),
        "gpu_hours": context["budget"].elapsed() / 3600,
        "limitations": ["仅开发集实验，未进行语义双评或独立审计"],
    }
    if completed:
        selected = next(
            row
            for row in completed
            if row["attempt"] == result["selection"]["attempt"]
        )
        result["scores"] = {
            name: value
            for name, value in selected["scores"].items()
            if isinstance(value, (int, float, str))
        }
        result["adapter"] = _export_adapter(context, selected, root)
        result["snapshot"] = {
            "path": selected["final_snapshot_path"],
            "sha256": selected["snapshot_file_hash"],
        }
    healthy = len(study["trials"]) == 24 and len(completed) >= 23
    result["status"] = "completed" if healthy else "failed"
    write_json(root / "experiment-result.json", result)
    return result, 0 if healthy else 3


def _run_search(root, run, settings, protocol):
    from .ara_research_runner import (
        _ResearchSession,
        _recover_search_attempts,
        _study_identity,
        run_search,
    )
    from .research_budget import StudyBudget

    search = root / "search"
    search.mkdir(exist_ok=True)
    identity = {**_study_identity(settings, protocol), "execution_scope": SCOPE}
    _recover_search_attempts(search, identity)
    history = (
        read_json(search / "study.json")
        if (search / "study.json").exists()
        else {}
    )
    remaining = 24 - len(history.get("trials", []))
    with StudyBudget(
        root / "budget.json", run["hours"] * 3600, devices=1
    ) as budget:
        write_status(root, "baseline", completed_attempts=24 - remaining)
        session = _ResearchSession(
            settings,
            protocol,
            search,
            budget,
            snapshots=remaining + 1,
        )
        context = session.context()
        context["identity"] = identity
        original_apply = context["apply"]

        def apply(parameters, attempt, phase):
            write_status(root, "search", attempt=attempt + 1, total=24)
            return original_apply(parameters, attempt, phase)

        context["apply"] = apply
        study = run_search(context, finalize=choose_experiment_candidate)
        write_status(root, "export")
        return _finish_experiment(context, study, root)


def verify_completed_result(root):
    """重复启动完成目录时只验证既有导出，避免覆盖或重跑。"""
    path = root / "experiment-result.json"
    if not path.exists():
        return None
    result = read_json(path)
    if result.get("status") != "completed":
        return None
    for name, expected in result["adapter"]["file_hashes"].items():
        if file_digest(Path(result["adapter"]["path"]) / name) != expected:
            raise ValueError(f"已完成实验的适配器文件损坏：{name}")
    if file_digest(result["snapshot"]["path"]) != result["snapshot"]["sha256"]:
        raise ValueError("已完成实验的快照损坏")
    return result


def validate_run_binding(run, settings, protocol):
    expected = {
        "max_wallclock_seconds": run["hours"] * 3600,
        "devices": 1,
        "members": [f"S2-{run['seed']}"],
    }
    if protocol["phase_budgets"] != {"experiment": expected}:
        raise ValueError("实验预算与冻结协议不一致")
    if (
        settings.ara_v3.method_id != "S2"
        or settings.seed != run["seed"]
        or settings.model != run["model"]
        or protocol["source_revision"] != run["source_revision"]
    ):
        raise ValueError("实验方法、种子、模型或源码与运行清单不一致")


def execute(root, prepare_only=False):
    from .ara_refinement_config import load_refinement_settings
    from .research_protocol import prepare_protocol

    run = read_run(root)
    completed = verify_completed_result(root)
    if completed is not None:
        print("实验已完成，导出文件校验通过。", flush=True)
        return 0
    ready = root / "prepared.json"
    if not ready.exists():
        write_status(root, "prepare")
        prepared = prepare_experiment(root, run)
    else:
        prepared = read_json(ready)
    settings = load_refinement_settings(root / "source" / RUNTIME_CONFIG)
    write_status(root, "preflight")
    protocol = prepare_protocol(settings, experiment=True)
    validate_run_binding(run, settings, protocol)
    if prepared["protocol_hash"] != protocol["protocol_hash"]:
        raise ValueError("准备结果与冻结协议不一致")
    write_json(ready, prepared, immutable=True)
    if prepare_only:
        write_status(root, "ready")
        return 0
    gpu_lock = Path(tempfile.gettempdir()) / "heretic-pro6000-cuda0.lock"
    with exclusive_lock(gpu_lock):
        check_hardware()
        result, code = _run_search(root, run, settings, protocol)
        write_status(root, result["status"], exit_code=code)
        return code


def _worker_main(root, prepare_only):
    """仅持有 worker 锁的进程写入状态，竞争启动不能覆盖正在运行的记录。"""
    code = 3
    try:
        code = execute(root, prepare_only)
    except KeyboardInterrupt:
        code = 130
        write_status(root, "interrupted", exit_code=code)
    except Exception as error:
        import traceback

        write_status(
            root,
            "failed",
            error=str(error),
            failure_category=type(error).__name__,
            exit_code=code,
        )
        traceback.print_exc()
    finally:
        write_json(
            root / "exit.json", {"exit_code": code, "finished_at": time.time()}
        )
    return code


def main():
    """后台 worker 保留真实退出码，TERM 中断也结清已用预算。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    root = args.run_dir.resolve()

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        with exclusive_lock(root / "worker.lock"):
            code = _worker_main(root, args.prepare_only)
    except (RuntimeError, OSError) as error:
        print(f"启动失败：{error}", file=sys.stderr)
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
