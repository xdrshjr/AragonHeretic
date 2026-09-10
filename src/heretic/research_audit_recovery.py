# SPDX-License-Identifier: AGPL-3.0-or-later
"""从一次性账本重建审计索引，保留不可变汇总历史。"""

from .ara_research_schema import digest, exclusive_lock, file_digest, write_json


def initialize_ledgers(plan, ledger_root):
    """在生成前登记所有角色；创建 pending 记录不会消费审计。"""
    from .research_audit import _open_ledger

    for role, manifest in plan["audit_manifests"].items():
        with exclusive_lock(ledger_root / f"{manifest}.lock"):
            _open_ledger(plan, role, ledger_root)


def recover_member_roles(plan, member, scope):
    """已消费角色只读恢复，不能再次申请 GPU 预算或重跑。"""
    from .research_audit import _ledger_key, _open_ledger, _recover_member

    ledger_root, evidence_root = scope
    output = {}
    for role, manifest in plan["audit_manifests"].items():
        with exclusive_lock(ledger_root / f"{manifest}.lock"):
            path, ledger = _open_ledger(plan, role, ledger_root)
            key = _ledger_key(plan, member, role)
            if ledger["members"][key]["state"] != "pending":
                output[role] = _recover_member(
                    path, ledger, key, evidence_root / f"{key}.json"
                )
    return output


def collect_results(plan, scope, execution_status):
    """仅恢复已落盘输出；未消费成员保留 pending，绝不调用模型。"""
    from .research_audit import _open_ledger, _recover_member

    root, ledger_root, evidence_root = scope
    members = {row["member_id"]: {} for row in plan["members"]}
    for member in plan["members"]:
        if member["availability"] == "unavailable":
            members[member["member_id"]] = {
                "status": "unavailable",
                "reason": member["reason"],
            }
    ledgers = {}
    for role, manifest in plan["audit_manifests"].items():
        with exclusive_lock(ledger_root / f"{manifest}.lock"):
            path, ledger = _open_ledger(plan, role, ledger_root)
            for key, state in ledger["members"].items():
                result = {"status": "not_run", "reason": "尚未消费"}
                if state["state"] != "pending":
                    result = _recover_member(
                        path, ledger, key, evidence_root / f"{key}.json"
                    )
                members[state["member_id"]][role] = result
            ledgers[role] = {
                "path": path.as_posix(),
                "sha256": file_digest(path),
            }
    result = {
        "plan_hash": plan["plan_hash"],
        "members": members,
        "ledgers": ledgers,
        "execution_status": "failed"
        if execution_status == "failed"
        else "passed",
    }
    write_json(
        root / "audit-history" / f"{digest(result)}.json",
        result,
        immutable=True,
    )
    write_json(root / "audit-results.json", result)
    return result
