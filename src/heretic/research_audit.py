# SPDX-License-Identifier: AGPL-3.0-or-later
"""集合冻结及逐成员一次消费；加载探针与审计正文访问分离。"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .ara_research_schema import (
    parse_candidate_lock,
    member_identity,
    research_version,
    digest,
    exclusive_lock,
    file_digest,
    read_json,
    resolve_artifact,
    write_json,
)
from .research_audit_recovery import new_audit_metadata, validate_member_storage


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def freeze_evaluation_plan(members: list[dict], protocol: dict) -> dict:
    """首次审计前锁定整个矩阵，保留失败对照与不可用成员。"""
    expected = {"B0"} | {
        f"{method}-{seed}"
        for method in ("B1", "B2", "S1", "S2")
        for seed in protocol["seeds"]
    }
    is_new = protocol.get("schema_version") == "cara-research-protocol-v3.1"
    if is_new:
        expected = {"B0"} | {
            f"{method}/{policy}/{seed}"
            for method, policy in (
                ("B1", "reject-v1"),
                ("B2", "reject-v1"),
                ("S1", "spectral-backtrack-v1"),
                ("S2", "spectral-backtrack-v1"),
            )
            for seed in (42, 43, 44)
        }
    names = [member["member_id"] for member in members]
    if len(set(names)) != len(names) or not expected.issubset(names):
        raise ValueError("EvaluationPlan 必须包含基座及完整方法/seed 矩阵")
    for member in members:
        _validate_member(member, protocol)
    budget = protocol["phase_budgets"].get("audit")
    if not budget or not budget.get("annotation_deadline"):
        raise ValueError("审计前必须冻结计算、标注预算及截止时间")
    _validate_audit_budget(budget, members)
    manifests = {
        role: digest(bundle)
        for role, bundle in protocol["roles"].items()
        if "audit" in role
    }
    if not manifests:
        raise ValueError("没有可审计的独立清单")
    plan = {
        "schema_version": "cara-research-evaluation-plan-v3",
        "protocol_hash": protocol["protocol_hash"],
        "required_level": protocol["required_level"],
        "audit_manifests": manifests,
        "evaluation_profile_hash": digest(protocol["generation_profiles"]),
        "base_shared_identity": digest(protocol["model_identity"]),
        "members": members,
        "execution_order": names,
        "budget": budget,
    }
    if is_new:
        plan.update(new_audit_metadata(protocol))
    plan["plan_hash"] = digest(plan)
    return plan


def _validate_audit_budget(budget, members):
    for key in ("max_gpu_hours", "max_wallclock_seconds"):
        if not isinstance(budget.get(key), (int, float)) or budget[key] <= 0:
            raise ValueError(f"审计缺少正数预算：{key}")
    allocation = budget.get("member_budgets", {})
    for member in members:
        if member["availability"] == "available":
            value = allocation.get(member["member_id"], {}).get(
                "max_wallclock_seconds", 0
            )
            if not isinstance(value, (int, float)) or value <= 0:
                raise ValueError("审计缺少可执行成员预算")
    annotation = budget.get("annotations", {})
    if not annotation.get("roles") or annotation.get("record_count", 0) <= 0:
        raise ValueError("审计缺少双评及裁决的标注人数/条数预算")


def _validate_member(member, protocol):
    is_new = protocol.get("schema_version") == "cara-research-protocol-v3.1"
    if is_new and member["member_id"] != "B0":
        validate_member_storage(member)
    status = member.get("availability")
    if status == "unavailable":
        if not member.get("reason"):
            raise ValueError("不可用矩阵成员必须记录原因")
        return
    if status != "available":
        raise ValueError("矩阵成员 availability 不合法")
    if member["member_id"] == "B0":
        if member.get("kind") != "base":
            raise ValueError("B0 必须是共享基座")
        return
    lock = parse_candidate_lock(member["candidate_lock"])
    if lock.protocol_hash != protocol["protocol_hash"]:
        raise ValueError("候选锁与审计协议身份不匹配")
    if member["candidate_lock_hash"] != digest(lock.model_dump()):
        raise ValueError("候选锁 hash 不匹配")
    expected = (
        f"{lock.method_id}/{lock.proposal_policy}/{lock.seed}"
        if is_new
        else f"{lock.method_id}-{lock.seed}"
    )
    if member["member_id"] != expected:
        raise ValueError("审计成员方法/seed 与候选锁不匹配")


def validate_plan(plan: dict) -> None:
    """验证冻结集合本身，拒绝审计后增删成员。"""
    payload = {key: value for key, value in plan.items() if key != "plan_hash"}
    if digest(payload) != plan.get("plan_hash"):
        raise ValueError("EvaluationPlan hash 不匹配")
    version = research_version(plan, "evaluation-plan")
    if version == "v3.1":
        expected = [f"S2/spectral-backtrack-v1/{seed}" for seed in (42, 43, 44)]
        if plan.get("primary_members") != expected:
            raise ValueError("主方法三 seed 映射不完整")
        if not set(expected).issubset(m["member_id"] for m in plan["members"]):
            raise ValueError("主方法成员在集合中缺失")
        if plan.get("primary_method") != {
            "method_id": "S2",
            "proposal_policy": "spectral-backtrack-v1",
        }:
            raise ValueError("集合主方法身份不匹配")
        for member in plan["members"]:
            if member["member_id"] == "B0":
                continue
            identity = member_identity(
                member["method_id"], member["proposal_policy"], member["seed"]
            )
            if any(member.get(k) != v for k, v in identity.items()):
                raise ValueError("成员物理存储键或结构身份不匹配")


@dataclass(frozen=True)
class AuditContext:
    """模型仅可在 reload_probe/evaluate 回调中访问。"""

    ledger_root: Path
    output_root: Path
    role: str
    reload_probe: Callable[[dict], dict]
    evaluate: Callable[[dict, str], dict]


def _ledger_key(plan, member, role):
    return digest(
        [
            plan["plan_hash"],
            member["member_id"],
            plan["audit_manifests"][role],
            plan["evaluation_profile_hash"],
        ]
    )


def _open_ledger(plan, role, root):
    manifest = plan["audit_manifests"][role]
    path = root / f"{manifest}.json"
    if path.exists():
        ledger = read_json(path)
        if ledger["evaluation_plan_hash"] != plan["plan_hash"]:
            raise ValueError("审计清单已独占绑定其他 evaluation plan")
    else:
        ledger = {
            "schema_version": "cara-research-audit-ledger-v3",
            "audit_manifest_hash": manifest,
            "evaluation_plan_hash": plan["plan_hash"],
            "members": {},
            "events": [],
        }
        if plan["schema_version"].endswith("-v3.1"):
            ledger["schema_version"] = "cara-research-audit-ledger-v3.1"
        for member in plan["members"]:
            if member["availability"] == "available":
                key = _ledger_key(plan, member, role)
                ledger["members"][key] = {
                    "member_id": member["member_id"],
                    "state": "pending",
                }
        write_json(path, ledger)
    return path, ledger


def _transition(path, ledger, key, updates):
    before = ledger["members"][key]["state"]
    after = updates["state"]
    allowed = {
        "pending": {"consumed"},
        "consumed": {"complete", "inconclusive"},
    }
    if after not in allowed.get(before, set()):
        raise ValueError(f"非法审计状态迁移：{before} -> {after}")
    ledger["members"][key].update(updates)
    ledger["events"].append(
        {
            "key": key,
            "before": before,
            "after": after,
            "time": _now(),
            "worker_pid": os.getpid(),
        }
    )
    write_json(path, ledger)


def evaluate_audit_member(plan, member, context: AuditContext) -> dict:
    """消费前独立重载；消费后任何中断都不得重新调用模型。"""
    validate_plan(plan)
    if member not in plan["members"] or member["availability"] != "available":
        raise ValueError("成员不属于冻结的可执行集合")
    role = context.role
    manifest = plan["audit_manifests"][role]
    with exclusive_lock(context.ledger_root / f"{manifest}.lock"):
        path, ledger = _open_ledger(plan, role, context.ledger_root)
        key = _ledger_key(plan, member, role)
        output = context.output_root / f"{key}.json"
        state = ledger["members"][key]
        if state["state"] != "pending":
            return _recover_member(path, ledger, key, output)
        probe = context.reload_probe(member)
        if probe.get("status") != "passed" or probe.get("audit_accessed"):
            raise ValueError("独立重载探针失败或提前访问审计正文")
        _transition(
            path,
            ledger,
            key,
            {
                "state": "consumed",
                "consumed_at": _now(),
                "reload_probe_hash": digest(probe),
            },
        )
        return _evaluate_once(
            plan, member, context, (path, ledger, key, output)
        )


def _evaluate_once(plan, member, context, transaction):
    path, ledger, key, output = transaction
    try:
        evidence = context.evaluate(member, context.role)
        if evidence.get("is_complete") is not True:
            raise ValueError("审计输出不完整")
        envelope = {
            "key": key,
            "plan_hash": plan["plan_hash"],
            "member_id": member["member_id"],
            "role": context.role,
            "profile_hash": plan["evaluation_profile_hash"],
            "evidence": evidence,
        }
        envelope["content_hash"] = digest(envelope)
        write_json(output, envelope, immutable=True)
    except Exception as error:
        _transition(
            path,
            ledger,
            key,
            {
                "state": "inconclusive",
                "reason": type(error).__name__,
            },
        )
        raise
    _transition(
        path,
        ledger,
        key,
        {
            "state": "complete",
            "output_hash": file_digest(output),
            "output_path": output.name,
        },
    )
    return envelope


def _recover_member(path, ledger, key, output):
    state = ledger["members"][key]
    if state["state"] == "inconclusive":
        return {"status": "inconclusive", "reason": state.get("reason")}
    if not output.is_file():
        if state["state"] == "complete":
            raise ValueError("已完成审计的冻结输出丢失")
        _transition(
            path,
            ledger,
            key,
            {
                "state": "inconclusive",
                "reason": "消费后输出未落盘，禁止重跑",
            },
        )
        return {"status": "inconclusive", "reason": "消费后输出未落盘"}
    envelope = read_json(output)
    content = {k: v for k, v in envelope.items() if k != "content_hash"}
    if (
        digest(content) != envelope.get("content_hash")
        or envelope["key"] != key
    ):
        raise ValueError("冻结审计输出被修改或身份不匹配")
    if envelope["evidence"].get("is_complete") is not True:
        raise ValueError("冻结审计输出不完整")
    if state["state"] == "consumed":
        _transition(
            path,
            ledger,
            key,
            {
                "state": "complete",
                "output_hash": file_digest(output),
                "output_path": output.name,
            },
        )
    elif state["output_hash"] != file_digest(output):
        raise ValueError("审计账本与冻结输出 hash 不一致")
    return envelope


def verify_reload_probe(
    command: list[str], output: Path, timeout: float
) -> dict:
    """在独立进程运行非审计固定探针，传播异常退出和超时。"""
    subprocess.run(command, check=True, timeout=timeout)
    report = read_json(output)
    if report.get("worker_pid") == os.getpid():
        raise ValueError("重载探针必须由独立进程执行")
    if report.get("status") != "passed" or report.get("audit_accessed"):
        raise ValueError("重载探针证据不合格")
    return report


def _verify_staging(member):
    root = Path(member["staging_path"])
    if file_digest(root / "staging.json") != member["staging_manifest_hash"]:
        raise ValueError("staging manifest 与冻结集合不一致")
    manifest = read_json(root / "staging.json")
    if manifest["candidate_lock_hash"] != member["candidate_lock_hash"]:
        raise ValueError("staging 候选锁不匹配")
    for relative, expected in manifest["core_hashes"].items():
        if file_digest(resolve_artifact(root, relative)) != expected:
            raise ValueError(f"staging 核心制品损坏：{relative}")
    return root, manifest


def _load_worker_model(request, settings=None):
    from .ara_research_runner import _load_settings
    from .model import Model

    settings = settings or _load_settings(request["config"])
    model = Model(settings)
    model.model.eval()
    member = request["member"]
    if member["member_id"] != "B0":
        from peft.utils.save_and_load import (
            load_peft_weights,
            set_peft_model_state_dict,
        )
        from .ara_refinement_capture import snapshot_factors, tensor_identity

        root, manifest = _verify_staging(member)
        weights = load_peft_weights(str(root), device="cpu")
        set_peft_model_state_dict(model.model, weights, adapter_name="default")
        if (
            tensor_identity(snapshot_factors(model.ara_targets))
            != manifest["factor_hash"]
        ):
            raise ValueError("独立 PEFT 重载未还原锁定因子")
    return model


def _worker_probe(request, settings=None):
    import torch
    from .ara_refinement_capture import disabled_targets
    from .utils import Prompt

    model = _load_worker_model(request, settings)
    member = request["member"]
    if member["member_id"] == "B0":
        prompt = Prompt("You are a helpful assistant.", "What is 2 + 2?")
        original = model.get_logits([prompt]).cpu()
        with disabled_targets(model.ara_targets):
            reference = model.get_logits([prompt]).cpu()
    else:
        root, _ = _verify_staging(member)
        prompt = read_json(root / "probe-input.json")
        original = model.get_logits(
            [Prompt(prompt["system"], prompt["text"])]
        ).cpu()
        reference = torch.load(
            root / "probe-logits.pt", map_location="cpu", weights_only=True
        )
    if not torch.allclose(
        original.float(), reference.float(), rtol=1e-5, atol=1e-6
    ):
        raise ValueError("独立重载非审计探针输出不一致")
    return {
        "status": "passed",
        "audit_accessed": False,
        "worker_pid": os.getpid(),
        "model_fingerprint": model.model_fingerprint,
    }


def _read_audit_body(request, protocol):
    from .research_protocol import _read_bound_body, is_audit_role

    role = request["role"]
    if not is_audit_role(role):
        raise ValueError("审计 worker 只能读取冻结审计角色")
    return _read_bound_body(
        protocol, Path(request["protocol_path"]).parent, role
    )


def _audit_role_evaluator(request, protocol):
    from .research_evaluation import RoleEvaluator
    from .utils import Prompt

    role = request["role"]
    side = role.rsplit(".", 1)[1]
    bodies = _read_audit_body(request, protocol)
    rows = protocol["roles"][role]["prompts"]
    # 仅初始化本角色，绝不通过双侧构造器提前打开另一份清单。
    evaluator = object.__new__(RoleEvaluator)
    evaluator.protocol, evaluator.role = protocol, role
    evaluator.prompts = {
        side: [Prompt(row["system"], row["text"]) for row in bodies]
    }
    evaluator.ids = {side: [row["prompt_id"] for row in rows]}
    return evaluator, rows, side


def _worker_refusal(request, model, protocol):
    import torch
    from .sequence_scores import score_sequence_kl

    evaluator, rows, side = _audit_role_evaluator(request, protocol)
    keywords = evaluator.generate_records(model, side, 100)
    semantic = evaluator.generate_records(model, side, 256)
    evidence = {
        "is_complete": True,
        "prompts": rows,
        "keyword_responses": keywords,
        "responses": semantic,
        "metrics": {"keywords": evaluator._keyword_rate(keywords)},
        "identity": {
            "anonymous_method_id": digest(request["member"]["member_id"])[:16],
            "rubric_hash": protocol["judge_identity"]["rubric_hash"],
        },
    }
    if side != "good":
        return evidence
    bundle = _audit_sequence_bundle(request, model, evaluator, protocol)
    scores = score_sequence_kl(model, bundle)
    evidence["metrics"].update(
        first_token_kl=scores.first_token_kl, sequence_kl=scores.value
    )
    evidence["sequence_identity"] = scores.identity.model_dump()
    evidence["reference_bundle"] = (request.get("base_evidence") or {}).get(
        "reference_bundle"
    )
    if request["member"]["member_id"] == "B0":
        bundle_path = Path(request["output"]).with_suffix(".reference.pt")
        torch.save(list(bundle.base_logits), bundle_path)
        evidence["reference_bundle"] = {
            "path": bundle_path.as_posix(),
            "file_hash": file_digest(bundle_path),
            "sequences": [vars(row) for row in bundle.sequences],
            "identity": bundle.identity.model_dump(),
        }
    return evidence


def _audit_sequence_bundle(request, model, evaluator, protocol):
    import torch
    from .ara_refinement_capture import TokenSequence
    from .ara_research_schema import ScoreIdentity
    from .sequence_scores import SequenceBundle, prepare_sequence_bundle

    if request["member"]["member_id"] == "B0":
        identity = ScoreIdentity(
            scorer_version="sequence-kl-v3",
            role=request["role"],
            prompt_ids=evaluator.ids["good"],
            generation_profile_hash=digest(protocol["generation_profiles"]),
            reference_sequence_hash=None,
            base_identity=digest(protocol["model_identity"]),
            candidate_identity="base",
            prompt_count=len(evaluator.ids["good"]),
            token_count=0,
        )
        return prepare_sequence_bundle(
            model, evaluator.prompts["good"], identity
        )
    reference = request["base_evidence"]["reference_bundle"]
    if file_digest(reference["path"]) != reference["file_hash"]:
        raise ValueError("共享基座序列 logits 已损坏")
    sequences = tuple(
        TokenSequence(
            row["prompt_id"],
            tuple(row["prompt_tokens"]),
            tuple(row["continuation"]),
        )
        for row in reference["sequences"]
    )
    logits = tuple(
        torch.load(reference["path"], map_location="cpu", weights_only=True)
    )
    return SequenceBundle(
        sequences, logits, ScoreIdentity.model_validate(reference["identity"])
    )


def _worker_ability(request, model, protocol):
    import lm_eval
    from lm_eval.models.huggingface import HFLM

    _read_audit_body(request, protocol)
    model_wrapper = HFLM(
        pretrained=model.model, tokenizer=model.tokenizer, batch_size=1
    )
    result = {"is_complete": True, "tasks": {}}
    for name, task in protocol["ability_tasks"].items():
        if name not in {"mmlu", "gsm8k", "ifeval"}:
            raise ValueError("能力任务未注册")
        if file_digest(task["task_config_path"]) != task["task_config_hash"]:
            raise ValueError("能力任务配置 hash 不匹配")
        output = lm_eval.simple_evaluate(
            model=model_wrapper,
            tasks=[str(Path(task["task_config_path"]).resolve())],
            limit=min(500, task["split_count"]),
            log_samples=True,
            random_seed=task["seed"],
            numpy_random_seed=task["seed"],
            torch_random_seed=task["seed"],
            **task.get("evaluation_kwargs", {}),
        )
        samples = output["samples"][task["task_id"]]
        actual_ids = [str(row["doc_id"]) for row in samples]
        if actual_ids != task["prompt_ids"]:
            raise ValueError(f"{name} 实际题目清单与预注册不匹配")
        records = []
        for row in samples:
            key = str(row["doc_id"])
            if row["doc_hash"] != task["document_hashes"][key]:
                raise ValueError(f"{name} 实际文档身份不匹配")
            value = row[task["sample_metric"]]
            if value not in (0, 1):
                raise ValueError("能力逐题指标必须为二元正确性")
            records.append(
                {
                    "prompt_id": key,
                    "scenario_group_id": task["scenario_groups"][key],
                    "correct": value,
                }
            )
        result["tasks"][name] = {
            "records": records,
            "split_count": task["split_count"],
            "prompt_ids": task["prompt_ids"],
        }
    return result


def _audit_worker(request_path):
    request = read_json(request_path)
    if file_digest(request["config"]) != request["config_hash"]:
        raise ValueError("审计 worker 配置在启动后发生改变")
    if request["operation"] == "probe":
        evidence = _worker_probe(request)
    else:
        protocol = read_json(request["protocol_path"])
        model = _load_worker_model(request)
        if request["role"].startswith("ability-audit"):
            evidence = _worker_ability(request, model, protocol)
        else:
            evidence = _worker_refusal(request, model, protocol)
    write_json(request["output"], evidence, immutable=True)


def _run_worker(request, root, timeout):
    request = {**request, "config_hash": file_digest(request["config"])}
    key = digest({k: v for k, v in request.items() if k != "output"})
    request_path = root / f"{key}.request.json"
    output = root / f"{key}.result.json"
    if output.exists() and request["operation"] == "probe":
        if request["member"]["member_id"] != "B0":
            _verify_staging(request["member"])
        return read_json(output)
    request["output"] = output.as_posix()
    write_json(request_path, request, immutable=True)
    subprocess.run(
        [sys.executable, "-m", "heretic.research_audit", str(request_path)],
        check=True,
        timeout=timeout,
    )
    return read_json(output)


def run_audit_phase(args, settings):
    """在训练进程外逐成员执行一次审计；完整基座输出按身份共享。"""
    from .research_protocol import prepare_protocol
    from .research_audit_recovery import initialize_ledgers, collect_results

    if not args.evaluation_plan:
        raise ValueError("audit 必须指定冻结 EvaluationPlan")
    plan = read_json(args.evaluation_plan)
    validate_plan(plan)
    protocol = prepare_protocol(settings)
    if plan["protocol_hash"] != protocol["protocol_hash"]:
        raise ValueError("审计计划与协议不匹配")
    root = Path(args.run_dir)
    evidence_root = root / "audit-evidence"
    ledger_root = (
        Path(settings.ara_v3.protocol_manifest).parent / "audit-ledgers"
    )
    with exclusive_lock(root / "audit-worker.lock"):
        initialize_ledgers(plan, ledger_root)
        result, code = _execute_audit_phase(
            args, settings, plan, (root, ledger_root, evidence_root)
        )
        collect_results(
            plan, (root, ledger_root, evidence_root), result["research_status"]
        )
    return result, code


def _execute_audit_phase(args, settings, plan, scope):
    from .ara_research_runner import StudyBudget

    root, ledger_root, evidence_root = scope
    devices = plan["budget"].get("devices", 2)
    limit = min(
        plan["budget"]["max_wallclock_seconds"],
        plan["budget"]["max_gpu_hours"] * 3600 / devices,
    )
    try:
        with StudyBudget(
            root / "audit-budget.json", limit, devices=devices
        ) as budget:
            _iterate_audit_members(
                args, settings, plan, (root, ledger_root, evidence_root, budget)
            )
    except (
        RuntimeError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        return {
            "phase": "audit",
            "research_status": "failed",
            "plan_hash": plan["plan_hash"],
            "reason": str(error),
        }, 2 if isinstance(error, ValueError) else 3
    return {
        "phase": "audit",
        "research_status": "inconclusive",
        "plan_hash": plan["plan_hash"],
    }, 0


def _iterate_audit_members(args, settings, plan, scope):
    root, ledger_root, evidence_root, budget = scope
    results = {}
    members = {row["member_id"]: row for row in plan["members"]}
    order = ["B0"] + [key for key in plan["execution_order"] if key != "B0"]
    for member_id in order:
        member = members[member_id]
        if member["availability"] != "available":
            results[member_id] = {
                "status": "unavailable",
                "reason": member["reason"],
            }
            continue
        results[member_id] = {}
        budget.check()
        results[member_id] = _evaluate_member_roles(
            args,
            settings,
            plan,
            member,
            (root, ledger_root, evidence_root, results),
        )
    return results


def _evaluate_member_roles(args, settings, plan, member, scope):
    from .ara_research_runner import StudyBudget
    from .research_audit_recovery import recover_member_roles

    root, ledger_root, evidence_root, results = scope
    output = recover_member_roles(plan, member, (ledger_root, evidence_root))
    pending = [role for role in plan["audit_manifests"] if role not in output]
    if not pending:
        return output
    member_id = member["member_id"]
    limit = plan["budget"]["member_budgets"][member_id]["max_wallclock_seconds"]
    path = root / "member-budgets" / f"{digest(member_id)}.json"
    with StudyBudget(
        path, limit, devices=plan["budget"].get("devices", 2)
    ) as budget:
        for role in pending:
            budget.check()
            context = _make_audit_context(
                args,
                settings,
                member,
                role,
                (root, ledger_root, evidence_root, results, budget),
            )
            output[role] = evaluate_audit_member(plan, member, context)
    return output


def _make_audit_context(args, settings, member, role, state):
    root, ledger_root, evidence_root, results, budget = state
    common = {
        "config": str(Path(args.config).resolve()),
        "member": member,
        "protocol_path": str(Path(settings.ara_v3.protocol_manifest).resolve()),
    }

    def probe(_):
        return _run_worker(
            {**common, "operation": "probe"},
            root / "workers",
            budget.limit - budget.elapsed(),
        )

    def evaluate(_, audit_role):
        base = None
        if member["member_id"] != "B0":
            envelope = results["B0"][audit_role]
            if "evidence" not in envelope:
                raise ValueError("共享基座审计输出不完整")
            base = envelope["evidence"]
        return _run_worker(
            {
                **common,
                "operation": "evaluate",
                "role": audit_role,
                "base_evidence": base,
            },
            root / "workers",
            budget.limit - budget.elapsed(),
        )

    return AuditContext(ledger_root, evidence_root, role, probe, evaluate)


if __name__ == "__main__":
    _audit_worker(sys.argv[1])
