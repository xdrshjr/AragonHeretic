"""核对真实全量预检结果，生成不含原始题目的验证记录。"""
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / "source/src"))

from heretic.ara_refinement_config import load_refinement_settings
from heretic.ara_research_schema import digest, file_digest, read_json, write_json
from heretic.pro6000_experiment import read_run, validate_run_binding
from heretic.pro6000_launch import worker_is_alive
from heretic.pro6000_prepare import (
    RUNTIME_CONFIG, SCOPE, check_hardware, prepare_experiment, read_pilot,
)
from heretic.research_protocol import prepare_protocol

run = read_run(root)
prepared = read_json(root / "prepared.json")
status = read_json(root / "status.json")
exit_record = read_json(root / "exit.json")
assert status["stage"] == "ready"
assert exit_record["exit_code"] == 0
assert not worker_is_alive(root)
assert not (root / "budget.json").exists()
assert not (root / "search/study.json").exists()
settings = load_refinement_settings(root / "source" / RUNTIME_CONFIG)
protocol_path = root / "protocol/protocol.json"
protocol = read_json(protocol_path)
validate_run_binding(run, settings, protocol)
original_hash = file_digest(protocol_path)

# 覆盖“协议已落盘、prepared.json 尚未落盘”的恢复准备路径。
again = prepare_experiment(root, run)
assert again["protocol_hash"] == prepared["protocol_hash"]
assert file_digest(protocol_path) == original_hash
_, pilot, _ = read_pilot(Path(run["pilot_record"]))
preserved = {}
for role in ("development", "mechanism-development"):
    for side in ("good", "bad"):
        name = f"{role}.{side}"
        preserved[name] = protocol["roles"][name]["body_hash"] == pilot["roles"][name]["body_hash"]
assert all(preserved.values())

all_hashes = []
counts = {}
for name, bundle in protocol["roles"].items():
    counts[name] = {"candidate": bundle["candidate_count"], "selected": bundle["selected_count"]}
    all_hashes += [row["normalized_text_hash"] for row in bundle["prompts"]]
assert len(all_hashes) == len(set(all_hashes)) == 800
assert settings.ara_calibration_size == 96
assert settings.ara_v3.monitor_samples == 64
assert settings.ara_v3.method_id == "S2"
assert settings.ara_v3.sweeps == 2
try:
    prepare_protocol(settings)
except ValueError as error:
    assert "对应实验入口" in str(error)
    formal_rejection = str(error)
else:
    raise AssertionError("研究入口错误接受了独立实验协议")

result = {
    "status": "passed",
    "scope": SCOPE,
    "source_revision": run["source_revision"],
    "run_dir": str(root),
    "protocol_hash": protocol["protocol_hash"],
    "protocol_file_sha256": original_hash,
    "runtime_config_sha256": file_digest(root / "source" / RUNTIME_CONFIG),
    "preparation_resume_is_idempotent": True,
    "preserved_pilot_bodies": preserved,
    "sample_counts": counts,
    "unique_prompt_count_across_all_roles": len(set(all_hashes)),
    "formal_entry_rejection": formal_rejection,
    "hardware_after_preflight": check_hardware(),
    "long_experiment_started": False,
    "gpu_budget_started": False,
    "preflight_exit": exit_record,
}
write_json(root / "oneclick-validation.json", result, immutable=True)
print(result)
