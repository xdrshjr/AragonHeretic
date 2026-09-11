# SPDX-License-Identifier: AGPL-3.0-or-later
"""一键冻结源码、后台启动与恢复 Pro 6000 实验。"""

from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .ara_research_schema import digest, exclusive_lock, read_json, write_json
from .pro6000_prepare import SCOPE

DEFAULT_ROOT = "/root/autodl-fs/heretic-runs"
DEFAULT_MODEL = "/root/autodl-fs/models/Qwen3.8-27B"
DEFAULT_PILOT = "/root/autodl-fs/heretic-runs/ara-v3-pro6000-smoke-20260911"


def freeze_source(project, root):
    """从 Git 提交归档源码，运行中更新主工作区不会改变实验。"""
    revision = subprocess.check_output(
        ["git", "-C", str(project), "rev-parse", "HEAD"],
        text=True,
        timeout=20,
    ).strip()
    archive = root / "source.tar"
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "archive",
            "--format=tar",
            f"--output={archive}",
            revision,
        ],
        check=True,
        timeout=120,
    )
    source = root / "source"
    source.mkdir()
    with tarfile.open(archive) as contents:
        contents.extractall(source, filter="data")
    archive.unlink()
    if not (source / "src/heretic/pro6000_experiment.py").is_file():
        raise ValueError("当前一键入口尚未提交，无法冻结完整实验源码")
    return revision


def create_run(project, options):
    """每次新实验使用新目录；配置参数在启动前一次性冻结。"""
    hours = options.hours if options.hours is not None else 48.0
    if not math.isfinite(hours) or hours <= 0:
        raise ValueError("实验小时预算必须为正的有限数")
    seed = options.seed if options.seed is not None else 42
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"ara-v3-pro6000-S2-{seed}-{stamp}-{uuid4().hex[:6]}"
    root = Path(options.run_root or DEFAULT_ROOT).resolve() / name
    root.mkdir(parents=True)
    revision = freeze_source(project, root)
    run = {
        "scope": SCOPE,
        "run_dir": str(root),
        "seed": seed,
        "hours": hours,
        "source_revision": revision,
        "model": str(Path(options.model or DEFAULT_MODEL).resolve()),
        "pilot_record": str(
            Path(options.pilot_record or DEFAULT_PILOT).resolve()
        ),
    }
    write_json(
        root / "run.json", {**run, "run_hash": digest(run)}, immutable=True
    )
    return root


def worker_is_alive(root):
    """使用 PID 加创建时间，避免把复用的 PID 当成本次 worker。"""
    import psutil

    path = root / "pid.json"
    if not path.exists():
        return False
    identity = read_json(path)
    try:
        process = psutil.Process(identity["pid"])
        return (
            process.create_time() == identity["created_at"]
            and process.status() != psutil.STATUS_ZOMBIE
        )
    except psutil.NoSuchProcess:
        return False


def describe_run(root):
    """读取进度而不加载模型；不存在的目录明确报错。"""
    from .pro6000_experiment import read_run

    run = read_run(root)
    status_path = root / "status.json"
    status = read_json(status_path) if status_path.exists() else {}
    timeout = root / "budget-timeout.json"
    if timeout.exists():
        status = {"stage": "failed", "error": "累计时间预算已用尽"}
    print(f"实验目录：{root}")
    print(f"源码提交：{run['source_revision']}")
    print(f"阶段：{status.get('stage', 'queued')}")
    print(f"进程存活：{worker_is_alive(root)}")
    study_path = root / "search/study.json"
    if study_path.exists():
        trials = read_json(study_path)["trials"]
        complete = sum(row["state"] == "COMPLETE" for row in trials)
        failed = sum(row["state"] == "FAIL" for row in trials)
        print(f"已登记 {len(trials)}/24，完成 {complete}，失败 {failed}")
    if status.get("error"):
        print(f"错误：{status['error']}")
    print(f"日志：{root / 'run.log'}")


def start_worker(root, prepare_only):
    """独立会话后台运行；双重锁阻止同一目录被重复启动。"""
    import psutil

    from .pro6000_experiment import read_run

    with exclusive_lock(root / "launch.lock"):
        run = read_run(root)
        if worker_is_alive(root):
            raise ValueError("本目录已有存活 worker，请查看状态")
        command = [
            sys.executable,
            "-m",
            "heretic.pro6000_experiment",
            "--run-dir",
            str(root),
        ]
        if prepare_only:
            command.append("--prepare-only")
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(root / "source/src")
        environment["PYTHONUNBUFFERED"] = "1"
        with (root / "run.log").open("ab") as log:
            process = subprocess.Popen(
                command,
                cwd=root / "source",
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        try:
            created_at = psutil.Process(process.pid).create_time()
        except psutil.NoSuchProcess:
            raise RuntimeError(
                f"worker 提前退出，请查看 {root / 'run.log'}"
            ) from None
        write_json(
            root / "pid.json",
            {
                "pid": process.pid,
                "created_at": created_at,
            },
        )
    print_start(root, run, process.pid, prepare_only)


def print_start(root, run, pid, prepare_only):
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / ("run_qwen38_27b_ara_v3_pro6000.sh")
    )
    print(f"已后台启动，PID：{pid}")
    print(f"模式：{'只准备和预检' if prepare_only else '全量 S2 / 24 次搜索'}")
    print(f"单卡累计时间上限：{run['hours']:g} 小时")
    print(f"实验目录：{root}")
    print(f"查看日志：tail -f '{root / 'run.log'}'")
    print(f"恢复运行：bash '{script}' --resume '{root}'")


def main():
    """默认 seed 42、48 小时；恢复时禁止悄悄修改冻结参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", type=Path, help="恢复既有实验目录")
    mode.add_argument("--status", type=Path, help="查看实验状态")
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="只准备全量协议和预检，不加载候选模型",
    )
    parser.add_argument("--seed", type=int, choices=[42, 43, 44])
    parser.add_argument("--hours", type=float, help="累计单卡小时上限，默认 48")
    parser.add_argument("--run-root", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--pilot-record", type=Path)
    args = parser.parse_args()
    try:
        if args.status:
            describe_run(args.status.resolve())
            return
        if args.resume:
            if any(
                getattr(args, name) is not None
                for name in (
                    "seed",
                    "hours",
                    "run_root",
                    "model",
                    "pilot_record",
                )
            ):
                raise ValueError("恢复必须沿用原配置，不能同时传入新实验参数")
            root = args.resume.resolve()
        else:
            root = create_run(Path(__file__).resolve().parents[2], args)
        start_worker(root, args.prepare_only)
    except (
        ValueError,
        RuntimeError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        print(f"启动失败：{error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
