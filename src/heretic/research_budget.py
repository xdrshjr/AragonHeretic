# SPDX-License-Identifier: AGPL-3.0-or-later
"""研究预算计费、阶段共享账本及 GPU 子进程超时回收。"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from .ara_research_schema import read_json, write_json

_TIMEOUT_LOCK = threading.Lock()


class ResearchBudgetExceeded(RuntimeError):
    """累计预算不足或独立 watchdog 到期。"""


def phase_budget_path(settings, protocol, phase, root):
    """pilot/消融按协议共享总账，正式搜索保留独立 study 总账。"""
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
        self.session = None
        self._state_lock = threading.RLock()

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
            self.watchdog.cancel()
            self._finish_session()
