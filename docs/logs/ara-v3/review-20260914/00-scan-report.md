# ARA v3.2 第 1+1 轮代码评审与修复报告

日期：2026-09-14。节点：#4。**结论：pass；未解决 P0=0、P1=0，可交 #5 验证与精确提交。** 本轮发现八项 P1，均已就地修复并通过独立 QA；另有两项 P2 保留。代码验收不构成新 GPU 进展、资源或研究效果验收。

## 评审范围与确认链

从黑板 `plan/current` 定位 [规格](../../../plans/ara-v2-refusal-optimization/spec.md)，以 v3.2 增量文件计划和上游实现为基础。进入节点时 HEAD 为 `0ab032c8acdee8a0f197d4ffba8bec1bad93caad`，已有 42 个跟踪文件工作树修改、6 个计划内新源码/测试及 23 个暂存删除；本节点不提交，不重写历史归档。

按 code-diagnosis 技能实行 Scanner→Reviewer→QA 三方确认。三名 Scanner 分别检查数值/事务/评分、预算/放行/双入口、协议/制品/统计；独立 Reviewer 确认八项并对两项作范围修正，QA 复核全部十项。最终关闭依据包含源代码检查、修复前失败用例、修复后回归及独立真实函数反例。扫描报告保留发现当时的代码位置与状态，最终状态以本报告和 QA 终验为准。

| 证据 | 内容 |
| --- | --- |
| [数值扫描](01-scanner-numerics.md) | 谱投影、有效权重回溯、恢复、评分与显存 |
| [放行扫描](02-scanner-gates.md) | 成本、账本、成员并发、正式与单卡入口 |
| [身份扫描](03-scanner-identity.md) | 新旧 schema、目标/角色、制品链与统计 |
| [独立审阅](04-independent-review.md) | 逐项复核可达性、规格依据及误报边界 |
| [最终 QA](05-qa.md) | 八项 P1 关闭证据、两项 P2 责任与限制 |
| [预算和放行修复说明](06-gates-fix.md) | 修复实现及针对性回归 |

未将合法数值失败、基线效果失败、旧五组零更新或尚未执行 GPU 研究误报为代码 P1；同成员重复预算问题限定在同一冻结 target 内，不禁止明确注册的独立研究。

## 已修复问题

| 编号与原扫描编号 | 级别 | 触发、影响与最终修复 | 主要代码位置 |
| --- | --- | --- | --- |
| ARA-REVIEW2-001 / NUM-001 | P1 | capture 超限后低峰阶段重置计数器，原 guard 漏检。现在阶段结束即检查采集峰值，失败不进入下一正常阶段，保留嵌套峰值与原异常 | `ara_refinement_capture.measure_research_phase` / `_check_phase_peak` |
| ARA-REVIEW2-002 / SCAN-GATE-001 | P1 | 同一原生文件中短 capture 可被选作完整 trial 等成本。现在逐项核对真实计时操作及必经分支，完整 trial、两次 replay、apply/reload/export 按冻结公式计费 | `research_budget._measurement_seconds` / `pilot_cost_prediction` |
| ARA-REVIEW2-003 / SCAN-GATE-002 | P1 | 自洽 source hash 无法证明当前目标、R2 完整校准及实际计费。现在绑定源协议、profile、预注册参数/调用、设备、结清区间与输出清单；必需资源/进展/重放均须成功 | `ara_pilot.validate_cost_source` / `_verify_stage_ledger` / `_verify_stage_call`，`StudyBudget.bind_stage` / `_fail_stage` |
| ARA-REVIEW2-004 / SCAN-GATE-003 | P1 | 相对 ledger 路径随 CWD 改变，同阶段可再次取得总额度。现在冻结绝对规范路径，拒绝父目录跳转和阶段路径冲突 | `ara_refinement_config.frozen_ledger_name`，`research_budget.phase_budget_path` |
| ARA-REVIEW2-005 / SCAN-GATE-004 | P1 | 同一目标成员换目录创建第二 study，重复预算及 24 次尝试。现在锁内原子登记 study 身份、唯一根目录及预算位置，双入口模型加载前申请；原目录恢复累计且不能丢账重置 | `research_budget.reserve_campaign_member` / `campaign_member`，正式 runner 与 `pro6000_experiment._run_search` |
| ARA-REVIEW2-006 / SCAN-GATE-005 | P1 | 资源/身份故障和尚未成功重放被发布为 COMPLETE，允许下一成员。现在错误分类持久化到 campaign，硬超时跨成员锁存，必需收尾成功后发布完成；普通效果失败仍保留 | `research_budget._blocking_trial` / `_latch_campaign_timeouts` / `campaign_search_status`，`ara_research_runner._finish_search` |
| ARA-REVIEW2-007 / IDENTITY-001 | P1 | 同 target 下评分器、精度、角色正文可改变，来源消费未完整核验。现在目标冻结完整 judge、量化/精度、各 profile 的角色内容身份，准备/执行/来源消费共用校验并明确要求新 schema | `research_protocol._validate_new_protocol` / `role_execution_identity`，`ara_research_schema`，`pro6000_prepare`，`ara_pilot._validate_source_context` |
| ARA-REVIEW2-008 / IDENTITY-002 | P1 | 旧验收可绑定新 reproduce，候选标签及空执行 hash 存在缺口。现在整链双向同版，新执行和 study hash 非空并与锁相等，旧版合法路径保留 | `ara_research_acceptance.validate_research_binding` / `_validate_passed_evidence`，`ara_research_schema` |

QA 最后发现 GATE-002 的额外反例：必需 `r2-backtrack-anchor2-resource` OOM 后，额外预注册的较窄资源项仍可产生 41.25 秒成本预测。最终补丁明确必需调用成功，并在异常、硬超时和孤立 worker 收尾保存失败分类。原反例现在拒绝；普通 comparison 数值 FAIL 仍允许，相同 comparison 改为 OOM 则拒绝。该补丁属于同一 P1 的闭环修复，不另行重复计数。

## 实际验证

**274 项不同测试全部通过**，分为最终 [273 项 CPU 回归](full-cpu-final.log)（2.413 秒）和 [1 项真实子进程预算测试](local-budget.log)（1.339 秒）。上游本轮交接为 250+1 项，本轮最终增加 23 项 CPU 测试；针对性重复执行不再次计入总数。

远端 CPU 使用开发服务器现有 Python 3.12.13、torch 2.13.0、transformers 5.16.1、peft 0.20.0、optuna 4.9.0、lm_eval 0.4.12，当前工作树源码通过内存加载器传入并隐藏 CUDA；没有更新远端项目源码或安装依赖。需要真实新解释器加载文件的 watchdog 测试单独在本地执行，验证自身后代被终止而无关进程存活。测试数据和模型均为人工夹具或小型测试模型。

| 核验 | 结果与证据 |
| --- | --- |
| 跨阶段显存 | [修复前](memory-before.log)复现失败，[修复后](memory-after.log)13 项通过，覆盖超限与原异常保留 |
| 目标/制品身份 | [跨版修复前](identity-binding-before.log)、[绑定修复后](identity-binding.log)、[关联回归](identity-protocol.log)43 项通过 |
| 来源协议版本 | [修复前](source-version-before.log)旧/未知/缺失版本三负例失败，[修复后](source-version-after.log)14 项通过 |
| 成本/账本/成员故障 | [修复前](gates-before.log)、[修复后](gates-after.log)，[最终成本专测](costs-final.log)7 项通过 |
| 最宽资源失败替代 | [修复前](resource-completion-before.log)3 项中 2 FAIL、1 ERROR；[修复后](resource-completion-after.log)17 项通过；新增三项最终归入 `test_pro6000_experiment.StageCompletionRegressionTests` |
| 独立 QA 反例 | 五项成本改用 capture 均拒绝；真实两进程抢同成员只有一个根获准；同根累计预算保留；非主成员硬超时 marker 阻断；通用制品双向混用拒绝，合法旧/新文件字节不变 |
| Linux/CLI | [7 项通过](entrypoints-final.log)：4 个模块帮助、准备 CLI 帮助及 2 个 shell 语法检查 |
| 静态和范围 | [验证记录](static-validation.json)：47 路径，43 个 Python 文件编译及 Ruff，25 个源码文件增量函数限制，`git diff --check` 均通过 |

规格要求的源码文件最多 800 行、增量函数最多 50 行/5 个业务参数/圈复杂度 10/嵌套 4 层/行宽 80 均核验。中间修复暴露的测试夹具兼容问题和函数复杂度超限均已修正，最终验证记录为绿；失败日志保留原始含义。宿主不支持异步等待工具的文件探针，工具失败已上报，实际验证使用短命令完成，不作为成功证据。

## 计划核对、惯例与 Git 隔离

[逐文件计划核对](file-plan.md)展开 47 项：41 个已跟踪修改和 6 个新文件，全部存在且动作符合；规格另行追加。评审修复全部位于既有计划源码/测试内，没有新增模块或扩展求解器职责。README 补充运行契约入口，审计材料落在允许的 `docs/logs/ara-v3/` 下。项目为 Python CLI，沿用模块命名和错误处理方式，中文说明保持一致；TypeScript、Tailwind 与网页 UI 不适用。

[当前 Git 状态](git-status.txt)仍有 42 个跟踪工作树修改（含规格）、23 个原有暂存删除及未跟踪目录/文件。原有 `.agentmesh`、`.bug-diagnosis`、`.claude-index`、`.idea`、`.run-log`、CLAUDE.md、旧 pilot 和论文构建材料不能整体混入下游提交。本节点只写任务运行材料和本轮交付内容；未运行 `git add` / `git commit`。

[基线](baseline.json)与最终静态核验确认 HEAD 未变，23 项暂存差异原始字节 SHA-256 保持 `ecc7027297b06b30062476f98150736a8ca51ae3ba46ab61fbb801074c2c0ff8`。行尾规范化提示不代表本节点修改索引；当前未暂存差异空白检查通过。历史提交归档中的原始日志空白问题保持原文。

## 遗留 P2 与研究限制

| 编号 | 状态、影响与责任时点 |
| --- | --- |
| ARA-REVIEW2-009 / NUM-002 | 未解决。monitor/预算等异常使已计算的部分回溯事件、误差和阶段诊断无法从 FAIL 行恢复；模型恢复正确且错误不会放行。运行实现后续在异常边界保存可审计事件，发表失败机制或完整成本结论前完成 |
| ARA-REVIEW2-010 / IDENTITY-003 | 未解决。未被正式路径调用的 `paired_group_interval` 缺退化状态和固定抽样身份；正式语义单侧函数另有正确处理。机制分析接入前增加版本、固定重采样与逐组身份，保留旧报告字节 |
| ARA-REVIEW-015 | 继续保留。未知释放时刻保守计费并披露 `stop_estimated`；真实成本分析前完善观测 |
| ARA-DESIGN2-007 | 继续保留。部分秩亏损后双零方向可能失活；记录实际秩，新增初始化消融必须预注册 |
| ARA-DESIGN-009 | 阶段条件。真实进展/资源校准、独立审计来源和抽样前提、跨模型及人工双评仍待执行 |

历史 ARA-REVIEW-011/012/013/014 已由本轮上游实现且经本轮相关回归核验，可关闭：真实预测误差、剩余快照磁盘预检、提前结束/重放成本检查点与 recovery 声明标签。历史文档保留原时态，当前问题账本以本次更新为准。

[旧 pilot 输入摘要核对](pilot-evidence-check.json)的 15 项均一致。旧五组零更新及关键词拒答率 1.0 的失败结论未改变；本节点没有执行新 GPU pilot、正式矩阵、模型下载/checksum、真实能力审计或人工双评。不能将 CPU 回归和代码通过解释为超过基线、极低拒答率或真实 search_readiness。

## 给 #5 / #6 的交接

#5 使用 [当前精确交付清单](delivery-manifest.json) 和 [原始字节摘要](SHA256SUMS) 逐文件验证后再精确提交，保护 23 项暂存删除和其他工作区材料。上游实现摘要记录当时字节；本轮修改后的当前字节以本交付清单为准，历史清单不重写。文件系统字节 SHA 与 Git 可能规范化换行后的对象 SHA 用途不同。

#6 可使用本轮 `review=pass`、open P0/P1 均为零的代码结论作流程决定。若后续只做代码提交验收，不要求在该节点补跑真实研究；进入实验则遵守规格 R1/R2/R3 证据和预算条件，补齐对应 P2 后再作相应机制/成本声明。

冻结字段、成本项及失败状态细节见 [运行补充](RUNBOOK-SUPPLEMENT.md)。机器可读验证见 [verification.json](verification.json)。任务平台完成返回与节点状态另在运行日志记录，本报告不把尚未执行的提交或研究阶段写成完成。
