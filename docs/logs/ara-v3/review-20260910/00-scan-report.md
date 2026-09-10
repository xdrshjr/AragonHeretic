# ARA sequential-v3 独立代码评审报告

评审日期：2026-09-10。结论：**pass；已修复 9 项 P1，无未解决 P0/P1，可进入下游精确提交。** 另顺带修复 1 项 P2，保留 5 项实现 P2 和上游 1 项研究资源 P2。

本报告支持代码交付与所列验证，不支持新方法效果成功。没有运行真实 GPU pilot、288-attempt 矩阵、真实能力审计或人工双评，也没有生成正式研究 adapter。

## 评审范围与确认链

按规格 File plan 扫描全部上游增量，同时读取未跟踪的新文件。三组 Scanner 分别检查算法/捕获、协议/审计和运行入口；跨组 Reviewer 独立核对触发与影响，QA 最终裁定。主代理通过故障注入、真实文件事务和子进程验证后完成修复。各角色为本次评审职责，不代表外部组织作出保证。

| 问题 | Scanner 证据 | 独立 Reviewer / QA | 修复及验证 |
| --- | --- | --- | --- |
| 001 快照异常恢复 | 算法组：恢复额外设备副本；异常跳过 flags | 运行组及协议 QA 确认 P1，限定为异常恢复风险 | 直接复制、flags 独立 finally；恢复复制异常终止搜索。故障用例先失败后通过 |
| 002 生成身份 | 协议组：非空前缀未冻结，缓存只绑定配置路径 | 算法 Reviewer / 协议 QA 确认 P1 | 禁止非空前缀；worker、探针绑定配置内容。空前缀仍合法 |
| 003 提升中断 | 协议组：逐成员 rename 早于集合终态 | 算法 Reviewer / 协议 QA 确认 P1 | 真实目录提升后再次 finalize 成功；修改报告仍拒绝 |
| 004 硬超时子进程 | 运行组：父进程退出不回收子进程，嵌套信号覆盖 | 算法 Reviewer / 协议 QA 确认 P1 | 独立 watchdog 与后代回收；真实测试证实无关兄弟进程存活 |
| 005 孤立 attempt | 运行组：预算准入先于 RUNNING 收尾 | 算法 Reviewer / 协议 QA 确认 P1 | 预算耗尽也先持锁核验并写 FAIL/interrupted，测试断言未加载模型 |
| 006 阶段预算 | 运行组：多目录各取得完整 pilot/消融预算 | 协议 Reviewer / 主代理 QA 确认 P1 | 协议/阶段共享总账和锁，方法/seed 成员校验；跨目录累计测试通过 |
| 007 审计账本 | 主代理：空/缺角色账本、未完成状态的 evidence 被信任 | 算法 Reviewer / 协议 QA 确认 P1 | 验证角色、成员键、完成状态、profile、输出与内容 hash；真实合法账本通过，缺失或不一致拒绝 |
| 008 审计恢复 | 协议组：异常无汇总；算法 Reviewer 补发现已完成成员被预算阻断 | 协议 QA 确认 P1 | 全角色 pending 初始化；纯文件恢复并保留历史汇总；已完成成员在预算前恢复；执行失败不能变为研究通过 |
| 009 finalize 文件 | 主代理：调用失败覆盖正式结论；Reviewer 补首次错误占位 | 算法 Reviewer / 协议 QA 确认 P1 | code 2/3 仅写 phase-errors；既有报告字节不变，首次错误纠正后可完成 |
| 010 子进程退出 | 运行组：SubprocessError 越过固定退出码 | Reviewer / QA 降为 P2，未认定伪成功 | 非零与超时统一退出 3，保留原因及具体元数据；已修 |

9 项 P1 均经独立复核接受，没有将推测的实际 CUDA OOM、任意重写整个制品链或缺测试本身冒充已发生故障。最终 QA 对本轮修复给出 PASS。

## 验证证据

| 验证 | 结果 | 文件 |
| --- | --- | --- |
| 上游基线全量 CPU 回归 | 165 项通过，1.661 秒 | `baseline-unittest.log` |
| 四项初始故障复现 | 2 failure + 2 error，符合预期；修后 4/4 通过 | `review-regressions-before.log`、`review-regressions-after.log` |
| 缓存/账本边界 | 修前 3 项失败、1 项合法输入通过 | `identity-regressions-before.log` |
| 审计异常汇总与成员预算恢复 | 修前分别复现异常，修后纳入全量通过 | `audit-recovery-before.log`、`member-budget-before.log` |
| 修后审计及验收定向回归 | 23 项通过 | `identity-recovery-verified.log` |
| 最终全量 CPU 回归 | 183 项通过，1.823 秒 | `final-unittest-verified.log` |
| 真实子进程硬预算 | 1 项通过，1.353 秒 | `process-budget-verified.log` |
| 静态及入口 | 36 个 Python 文件 Ruff/语法；12 个新增源码模块尺寸/复杂度/行宽；diff whitespace、bash 语法、两类 CLI 帮助、缺协议退出 2，全部符合预期 | `static-validation.json` |

最终覆盖 **184 项不同测试**。远端运行使用已存在的环境；当前本地源码与测试通过内存 importer 加载，未覆盖远端项目或上游隔离目录中的源文件，静态夹具沿用其相同版本文件。真实子进程测试在本地当前源码执行，避免子进程误加载远端旧源码。测试中的目录、响应、标签与模型均为夹具，不是研究结果。

中间失败完整保留：`identity-recovery-after.log` 中有一次本地 torch 缺失导致的模型模块 patch 失败，后在有真实依赖的环境通过；`final-unittest.log` 中曾误拒绝 v3 模板合法空前缀，修正后完成全量复验。这两份不能作为通过证据。

## 剩余 P2 与交接

1. `prediction_discrepancy` 尚未接入事件；P3 机制分析前补齐。
2. 恢复磁盘预检重新要求完整快照空间，可能过度拒绝；长任务续跑前改为增量空间计算。
3. 成本检查点未补全提前结束和开发评估/重放跨点；正式成本曲线报告前补齐。
4. recovery 通过仍被标为 `sample_only`；不能据此宣称已达到样本 1% 门槛。
5. 异常进程退出时间未知时使用带 `stop_estimated` 的保守计费，可能过计空闲时间；不能当作真实 GPU 占用。
6. 延续 ARA-DESIGN-009：双卡真实资源、独立审计数据与抽样前提、跨模型复验、人工双评仍待后续实验落实。

精确提交须覆盖上游清单、两个新增拆分模块及本轮规格/证据。当前 23 个旧论文/旧规格暂存删除与本任务无关，运行时目录、IDE/索引及其他论文产物也不应混入。本节点未暂存或提交，未改动既有索引。

完整问题编号、修复状态和 Git 摘要同时写入规格末尾“第 1+1 轮代码评审”。下表逐项列出 File plan 覆盖；完整文件摘要在 `static-validation.json`。

## 文件计划逐项核对

<!-- 本轮静态验证生成文件表 -->

| 规划路径 | 核对结果 |
| --- | --- |
| src/heretic/ara_refinement_config.py | 已按计划创建/修改；路径存在 |
| src/heretic/ara_refinement_capture.py | 已按计划创建/修改；路径存在 |
| src/heretic/ara_refinement.py | 已按计划创建/修改；路径存在 |
| src/heretic/ara_research_runner.py | 已按计划创建/修改；路径存在 |
| src/heretic/research_protocol.py | 已按计划创建/修改；路径存在 |
| src/heretic/research_evaluation.py | 已按计划创建/修改；路径存在 |
| src/heretic/research_audit.py | 已按计划创建/修改；路径存在 |
| src/heretic/sequence_scores.py | 已按计划创建/修改；路径存在 |
| src/heretic/ara_research_acceptance.py | 已按计划创建/修改；路径存在 |
| src/heretic/ara_research_schema.py | 已按计划创建/修改；路径存在 |
| src/heretic/config.py | 已按计划创建/修改；路径存在 |
| src/heretic/main.py | 已按计划创建/修改；路径存在 |
| src/heretic/trial_methods.py | 已按计划创建/修改；路径存在 |
| src/heretic/model.py | 已按计划创建/修改；路径存在 |
| src/heretic/ara_runtime.py | 已按计划创建/修改；路径存在 |
| src/heretic/protocol_data.py | 已按计划创建/修改；路径存在 |
| src/heretic/acceptance_export.py | 已按计划创建/修改；路径存在 |
| src/heretic/artifact_schema.py | 已按计划创建/修改；路径存在 |
| src/heretic/reproduce.py | 已按计划创建/修改；路径存在 |
| src/heretic/workflow.py | 已按计划创建/修改；路径存在 |
| src/heretic/utils.py | 已按计划创建/修改；路径存在 |
| config.qwen38-27b-cara-v2.toml | 已按计划创建/修改；路径存在 |
| config.qwen38-27b-cara-v3-96.toml | 已按计划创建/修改；路径存在 |
| scripts/run_ara_research_96.sh | 已按计划创建/修改；路径存在 |
| scripts/prepare_ara_research_protocol.py | 已按计划创建/修改；路径存在 |
| tests/test_ara_refinement.py | 已按计划创建/修改；路径存在 |
| tests/test_ara_refinement_capture.py | 已按计划创建/修改；路径存在 |
| tests/test_ara_research_runner.py | 已按计划创建/修改；路径存在 |
| tests/test_research_evaluation.py | 已按计划创建/修改；路径存在 |
| tests/test_sequence_scores.py | 已按计划创建/修改；路径存在 |
| tests/test_ara_research_acceptance.py | 已按计划创建/修改；路径存在 |
| tests/test_research_audit.py | 已按计划创建/修改；路径存在 |
| tests/test_protocol_data.py | 已按计划创建/修改；路径存在 |
| tests/test_config.py | 已按计划创建/修改；路径存在 |
| tests/test_trial_methods.py | 已按计划创建/修改；路径存在 |
| tests/test_acceptance_export.py | 已按计划创建/修改；路径存在 |
| tests/test_reproduce.py | 已按计划创建/修改；路径存在 |
| README.md | 已按计划创建/修改；路径存在 |
| docs/logs/ara-v3/<run-id>/ | 已按计划创建/修改；路径存在 |

评审必要新增：src/heretic/research_budget.py、src/heretic/research_audit_recovery.py，职责与原因见规格补充。
