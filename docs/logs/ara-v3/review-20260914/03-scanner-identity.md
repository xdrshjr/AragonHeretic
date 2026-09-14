# Scanner：研究身份、统计与制品分派复核

日期：2026-09-14。职责：code-diagnosis Scanner，只读源码，不修复、不操作任务平台、不访问含凭据提示、不派生代理。依据 `docs/plans/ara-v2-refusal-optimization/spec.md` v3.2；制品新版本仍为 v3.1。

本轮发现：P0 0 项、P1 2 项、P2 1 项。下列为待独立 Reviewer/QA 复核的 Scanner 结论，不能替代最终裁定。

## IDENTITY-001：目标契约未绑定真实评分器、计算精度及开发正文

- 优先级：P1；类别：BUG / 研究身份完整性。
- 主要位置：`src/heretic/research_protocol.py:373`，尤其 385–406；`src/heretic/ara_research_schema.py:172`；关联 `src/heretic/ara_refinement_config.py:348`。
- 契约：规格 215 行要求 R1/R2 development 的 ID、顺序、正文 hash 和评分配置完全一致；489–500 行要求目标执行契约固定模型/生成/scorer/资源等执行条件，变更后重新取得 readiness。

`_validate_new_protocol` 比较目标契约和协议时，只比较 model/tokenizer/source_files/package_versions/generation_profiles/initialization_sources/role_layout。契约的 `scorer_identity` 仅做非空检查，实际评分器读取的是 `protocol["judge_identity"]`，没有两者对照。`quantization`、`dtype` 也没有进入目标契约严格字段，协议中的实际值只与本次 settings 对照。角色映射仅保存和比较 `prompt_id`，不绑定 development 正文、system 或规范化文本摘要。

可达路径：独立准备 CLI 接受 JSON → `build_research_manifest` → `_protocol_payload` → `_validate_new_protocol`。新协议/新正文各自 hash 正确即可通过。随后 `prepare_protocol` 的 `_validate_runtime_identity`（438–480 行）只校验本次设置与本次协议，不能发现相对于原 target 的漂移；`RoleEvaluator.__call__`（525 行）直接采用本次 `judge_identity.prefix_scorer`。readiness 检查（`ara_pilot.validate_phase_readiness`）重算原始证据并对照目标契约，不对照新消费协议中被遗漏的这些字段。因此旧 readiness 可以继续证明同一 target，但实际新运行已经更换评分标准、精度或开发问题内容。

离线复现通过真实 `build_research_manifest`，没有 mock 该构建器或 `_validate_new_protocol`：使用现有 `test_ara_pilot.contract_fixture` 的阶段注册/角色映射，补齐正常 model/tokenizer 文件摘要、生成 profile、抽样字段，给八个开发角色写入独立的合规 JSON（每角色数量符合 smoke，所有 ID/内容互斥）。同一个 target 分别构建原始协议、修改 refusal_prefixes 的协议、将 nf4/bfloat16 改为 none/float32 的协议、仅修改 development 正文的协议。四份均构建和验证成功：

```text
original BUILT_AND_VALIDATED target=2e843e762817 protocol=26586b0788e5
changed-judge BUILT_AND_VALIDATED target=2e843e762817 protocol=993cf824148e
changed-precision BUILT_AND_VALIDATED target=2e843e762817 protocol=8151d7df5269
changed-development-text BUILT_AND_VALIDATED target=2e843e762817 protocol=42e17c2f12fd
SAME_TARGET True
CHANGED_BODY_HASH True
```

摘要随 created_at 等字段改变，不要求复核时同一 protocol hash；关键断言是 target 相同、四份验证通过、development body_hash 不同。源码清单非空/文件 hash 正确本身不能绑定评分参数，后者位于准备 JSON。

建议：在目标契约里冻结精度/量化、可实际使用的 scorer 身份及各 profile 的角色来源/内容摘要；明确 `scorer_identity` 与 `judge_identity` 的字段映射并强制相等；在新协议构建和运行前双重验证。R1/R2 的 development 需比较完整有序题目/正文/system 身份。为每类漂移增加“不变 target 被拒绝”的回归。单卡 `_preparation_payload` 的旧角色保护并不能替代双卡公开准备入口的校验。

## IDENTITY-002：旧 acceptance 可绑定新版 reproduce，新增执行身份未经验证

- 优先级：P1；类别：BUG / 版本分派与制品完整性。
- 位置：`src/heretic/ara_research_acceptance.py:49`–55；关联 `src/heretic/ara_research_schema.py:564`–588。
- 契约：规格 494 行要求新旧记录精确版本分派，禁止混用或给旧制品补字段变成新身份。

`validate_research_binding` 仅当 report 是 v3.1 时检查 reproduce 是 v3.1 并对照两个执行 hash。反向组合（v3 report + v3.1 reproduce）没有拒绝。新 reproduce 的解析器要求两个字段存在，却既不检查它们非空，也不与旧候选关联。结果可把旧正式验收/旧候选绑定到任意声称的新执行身份。

可达路径：`load_bound_acceptance` → `validate_acceptance_reproduce_binding` → `validate_research_binding`；`verify_research_artifact_graph` 也使用同一绑定检查。只要提供旧 acceptance 的正确文件 hash 和原 core 清单，这一错误不会被文件摘要补救。

实际离线复现（现有夹具，无模型）：

```python
from test_research_audit import evaluation_fixture
from test_ara_research_acceptance import _passed_report
from heretic.ara_research_schema import digest, RefinementParameters
from heretic.ara_research_acceptance import validate_research_binding

lock = next(m for m in evaluation_fixture()["members"]
            if m["member_id"] == "S2-42")["candidate_lock"]
report = _passed_report(lock, {"weights.bin": "hash"})
repro = {
    "schema": "cara-research-reproduce-v3.1",
    "protocol_hash": lock["protocol_hash"],
    "candidate_lock_hash": digest(lock),
    "parameters": RefinementParameters.model_validate(lock["parameters"]).envelope(),
    "acceptance_sha256": "hash",
    "core_hashes": report["core_hashes"],
    "execution_identity_hash": "unverified-execution",
    "study_execution_hash": "unverified-study",
}
validate_research_binding(report, repro)  # 当前未抛错
```

实际输出：`MIXED_VERSION_ACCEPTED cara-research-acceptance-v3 cara-research-reproduce-v3.1`。

建议：无条件比较 acceptance/reproduce/candidate 的对应精确版本，再按代次验证身份；新增 hash 字段至少要求非空并核验其绑定。增加两个交叉版本方向、空执行 hash 及整条通用文件加载链回归。现有新增测试只覆盖同为 v3.1 时修改 execution hash。

## IDENTITY-003：双侧机制 bootstrap 未实现本轮退化边界和重采样身份

- 优先级：P2；类别：BUG / 统计证据。
- 位置：`src/heretic/research_evaluation.py:249`–267。
- 契约：规格 331 行同时约束机制双侧区间与语义单侧区间：配对差值全相同或重采样退化时 inconclusive，保存逐组配对值、重采样身份及分析类型。

新 `paired_semantic_interval` 已处理退化，但既有 `paired_group_interval` 只检查组数，小于两组之外均返回数值区间；它仍允许任意 seed，并不保存配对数据、重采样 hash 或分析类型。该函数本轮未修改，但规格新要求明确覆盖这条现有机制分析入口。

实际复现：

```python
paired_group_interval([
    {"scenario_group_id": str(i), "left": 0, "right": 1}
    for i in range(10)
])
# {'n': 10, 'difference': -1.0,
#  'interval': [-1.0, -1.0], 'seed': 20260910}
```

相同差值产生零宽区间且没有 inconclusive 标记，调用方可能将之作为机制优越性证据。当前无正式流程调用该函数，故定 P2；未发现已输出错误研究结论。

建议：保留旧报告原文，通过明确分析版本给新机制路径固定 seed/10,000 次重采样/线性分位数，补齐差值和分布退化校验及可核验重采样记录，不回写历史结论。

## 检查范围与未发现问题的部分

| 文件 | Scanner 结论 |
| --- | --- |
| `ara_research_schema.py` | 关联 IDENTITY-001/002；study/trial 父身份分离与固定参数摘要没有另外发现可达问题。 |
| `research_protocol.py` | IDENTITY-001；跨角色内容去重、完整 fit 候选数量与冻结抽样顺序检查存在。 |
| `trial_methods.py` | 本轮参数包络/manifest 分派未发现独立问题。 |
| `reproduce.py` | 公开读取入口会拒绝未知 research schema，未发现独立问题。 |
| `workflow.py` | v3/v3.1 model gate 显式分派，未发现独立问题。 |
| `artifact_schema.py` | 受到 IDENTITY-002 下层绑定漏洞影响；新增整图校验分派无独立发现。 |
| `acceptance_export.py` | 委托完整重现链检查，未发现独立问题。 |
| `ara_research_acceptance.py` | IDENTITY-002；新版 recovery_supported 与逐 seed 主方法汇总符合本轮新增约束。 |
| `research_audit.py` | 新成员存储键、消费前标记、已落盘输出恢复未发现本轮独立问题。 |
| `research_audit_recovery.py` | 已消费角色只读恢复与新版结果索引未发现本轮独立问题。 |
| `research_evaluation.py` | IDENTITY-003；新增语义单侧区间的方向、按组排序配对、固定 seed、退化和缺成员处理未发现独立问题。 |

对应新增测试已检查，存在上述负例缺口。实际运行 `test_research_evaluation` 和 `test_ara_research_acceptance.NewResearchArtifactTests`，共 **13 项通过，0 失败、0 错误，0.360 秒**。三个发现的反例另外执行，均得到上列输出。环境为本地 Python 3.14.6，具备 NumPy/Pydantic，缺 torch/optuna/peft/transformers；本 Scanner 未声称运行完整模型、通用 workflow 导入测试或 GPU 实验。未修改源码、测试、Git 索引或历史制品。
