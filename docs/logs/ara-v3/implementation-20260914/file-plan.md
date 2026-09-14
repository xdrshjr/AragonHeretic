# 本轮逐文件交付清单

当前规格增量表展开为 47 个文件，全部已实施；另交付规格补充和本目录验证记录。

| 路径 | 计划动作 | 当前状态 | 职责与验证 |
| --- | --- | --- | --- |
| `src/heretic/ara_proposal.py` | 新建 | 新增未暂存 | 因子级谱投影、统一缩放、权重插值、rank 截断；不访问模型或数据源 |
| `src/heretic/ara_backtracking.py` | 新建 | 新增未暂存 | 固定 α 的整组事务、详细拒绝原因、异常恢复与预算检查 |
| `src/heretic/ara_pilot.py` | 新建 | 新增未暂存 | 由原生证据生成有效更新、开发进展和资源放行记录 |
| `src/heretic/ara_refinement.py` | 修改 | 工作树修改 | 分离原始求解与部署策略，接入回溯和真实预测误差，保留旧策略 |
| `src/heretic/ara_refinement_config.py` | 修改 | 工作树修改 | 策略、计费设备、新版执行身份、事件和 pilot schema；旧 hash 原样校验 |
| `src/heretic/ara_research_schema.py` | 修改 | 工作树修改 | 策略、计费设备、新版执行身份、事件和 pilot schema；旧 hash 原样校验 |
| `src/heretic/research_protocol.py` | 修改 | 工作树修改 | 策略、计费设备、新版执行身份、事件和 pilot schema；旧 hash 原样校验 |
| `src/heretic/ara_research_runner.py` | 修改 | 工作树修改 | 零更新状态、策略绑定重放、回溯计费、成本检查点与放行检查 |
| `src/heretic/research_budget.py` | 修改 | 工作树修改 | 零更新状态、策略绑定重放、回溯计费、成本检查点与放行检查 |
| `src/heretic/pro6000_prepare.py` | 修改 | 工作树修改 | 单卡新运行也使用进展放行；旧恢复保持旧身份；comparison 导出明确效果状态 |
| `src/heretic/pro6000_experiment.py` | 修改 | 工作树修改 | 单卡新运行也使用进展放行；旧恢复保持旧身份；comparison 导出明确效果状态 |
| `src/heretic/pro6000_launch.py` | 修改 | 工作树修改 | 单卡新运行也使用进展放行；旧恢复保持旧身份；comparison 导出明确效果状态 |
| `src/heretic/continuation_scores.py` | 修改 | 工作树修改 | 精确因果位置、完整词表的分块评分；逐项与原值核对 |
| `src/heretic/sequence_scores.py` | 修改 | 工作树修改 | 精确因果位置、完整词表的分块评分；逐项与原值核对 |
| `src/heretic/ara_refinement_capture.py` | 小范围修改 | 工作树修改 | 分阶段资源峰值与剩余快照预检，不改变捕获语义 |
| `src/heretic/ara_runtime.py` | 小范围修改 | 工作树修改 | 分阶段资源峰值与剩余快照预检，不改变捕获语义 |
| `src/heretic/trial_methods.py` | 修改 | 工作树修改 | 执行身份传递、旧新读取分派、recovery 标签修正；保留审计规则 |
| `src/heretic/reproduce.py` | 修改 | 工作树修改 | 执行身份传递、旧新读取分派、recovery 标签修正；保留审计规则 |
| `src/heretic/ara_research_acceptance.py` | 修改 | 工作树修改 | 执行身份传递、旧新读取分派、recovery 标签修正；保留审计规则 |
| `src/heretic/workflow.py` | 小范围修改 | 工作树修改 | `validate_reproduction_model` 显式分派 v3/v3.1，防止新制品进入旧 model_fingerprint 分支 |
| `src/heretic/research_audit.py` | 修改 | 工作树修改 | 新 method/policy/seed 成员标识、v3.1 读取与正式/比较制品分派；旧账本和制品不得迁移重写 |
| `src/heretic/research_audit_recovery.py` | 修改 | 工作树修改 | 新 method/policy/seed 成员标识、v3.1 读取与正式/比较制品分派；旧账本和制品不得迁移重写 |
| `src/heretic/artifact_schema.py` | 修改 | 工作树修改 | 新 method/policy/seed 成员标识、v3.1 读取与正式/比较制品分派；旧账本和制品不得迁移重写 |
| `src/heretic/acceptance_export.py` | 修改 | 工作树修改 | 新 method/policy/seed 成员标识、v3.1 读取与正式/比较制品分派；旧账本和制品不得迁移重写 |
| `src/heretic/research_evaluation.py` | 修改 | 工作树修改 | 新语义单侧配对区间、固定重采样参数、退化状态；不变更旧报告结论 |
| `scripts/prepare_ara_research_protocol.py` | 修改 | 工作树修改 | 冻结 role_layout、profile 映射和无环目标执行契约，传递新的准备输入 |
| `config.qwen38-27b-cara-v3-96.toml` | 修改 | 工作树修改 | 新运行显式选择新策略与阶段；退出码透传，不覆盖冻结旧配置 |
| `scripts/run_ara_research_96.sh` | 修改 | 工作树修改 | 新运行显式选择新策略与阶段；退出码透传，不覆盖冻结旧配置 |
| `scripts/run_qwen38_27b_ara_v3_pro6000.sh` | 修改 | 工作树修改 | 新运行显式选择新策略与阶段；退出码透传，不覆盖冻结旧配置 |
| `tests/test_ara_proposal.py` | 新建 | 新增未暂存 | 数值反例、回溯事务、旧零更新证据不得放行 |
| `tests/test_ara_backtracking.py` | 新建 | 新增未暂存 | 数值反例、回溯事务、旧零更新证据不得放行 |
| `tests/test_ara_pilot.py` | 新建 | 新增未暂存 | 数值反例、回溯事务、旧零更新证据不得放行 |
| `tests/test_ara_refinement.py` | 扩展 | 工作树修改 | 零/非零更新、两入口、恢复和导出状态一致 |
| `tests/test_ara_research_runner.py` | 扩展 | 工作树修改 | 零/非零更新、两入口、恢复和导出状态一致 |
| `tests/test_pro6000_experiment.py` | 扩展 | 工作树修改 | 零/非零更新、两入口、恢复和导出状态一致 |
| `tests/test_pro6000_export.py` | 扩展 | 工作树修改 | 零/非零更新、两入口、恢复和导出状态一致 |
| `tests/test_sequence_scores.py` | 扩展 | 工作树修改 | 流式/原始数值一致、EOS、长 prompt、旧新身份不混用 |
| `tests/test_refusal_log_odds.py` | 扩展 | 工作树修改 | 流式/原始数值一致、EOS、长 prompt、旧新身份不混用 |
| `tests/test_reproduce.py` | 扩展 | 工作树修改 | 流式/原始数值一致、EOS、长 prompt、旧新身份不混用 |
| `tests/test_workflow.py` | 扩展 | 工作树修改 | 通过通用重现入口读取两版制品；模型/执行身份错误及未知 schema 拒绝 |
| `tests/test_research_audit.py` | 扩展 | 工作树修改 | 新成员/旧 schema 分派、无环 hash、真实角色布局扩充、统计单双侧与退化、profile 计数边界 |
| `tests/test_ara_research_acceptance.py` | 扩展 | 工作树修改 | 新成员/旧 schema 分派、无环 hash、真实角色布局扩充、统计单双侧与退化、profile 计数边界 |
| `tests/test_acceptance_export.py` | 扩展 | 工作树修改 | 新成员/旧 schema 分派、无环 hash、真实角色布局扩充、统计单双侧与退化、profile 计数边界 |
| `tests/test_research_evaluation.py` | 扩展 | 工作树修改 | 新成员/旧 schema 分派、无环 hash、真实角色布局扩充、统计单双侧与退化、profile 计数边界 |
| `tests/test_config.py` | 扩展 | 工作树修改 | 新成员/旧 schema 分派、无环 hash、真实角色布局扩充、统计单双侧与退化、profile 计数边界 |
| `tests/test_protocol_data.py` | 扩展 | 工作树修改 | 新成员/旧 schema 分派、无环 hash、真实角色布局扩充、统计单双侧与退化、profile 计数边界 |
| `README.md` | 后续修改/生成 | 工作树修改 | 使用说明与原生事件、阶段成本、对照差值、放行报告、哈希清单 |

## 额外交付文档及日志

- 规格仅在实施发现节追加本轮说明，保留上游 v3.2 及历史记录。
- 本目录 SUMMARY.md、RUNBOOK.md、file-plan.md、verification.json、SHA256SUMS，以及全部原始验证日志。
- .agentmesh 下验证器和任务平台运行时文件不属于源代码交付清单。

暂存区原有 23 项删除与此前保存的 initial-staged.bin 字节一致；HEAD 未变化。本节点没有暂存或提交。
