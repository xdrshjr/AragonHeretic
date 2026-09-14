# 本轮代码评审后的运行契约补充

本文件补充上游 [运行手册](../implementation-20260914/RUNBOOK.md)。这些是代码审查中补齐的冻结及验证要求，不是新的 GPU 放行记录。旧 v3 制品继续按旧版本读取；本轮尚未发布的新 v3.1 输入必须满足完整契约。

## 目标契约必须绑定评分和数据正文

`cara-target-execution-v1` 增加顶层 `quantization`、`dtype`、`role_identities`。前两项与各后代协议及实际运行设置逐项相等；`scorer_identity` 保存完整 `judge_identity` 对象，不能只放版本标签。

`role_identities` 按 `smoke`、`full-calibration` 分开，每个非审计角色保存三项摘要：

| 字段 | 计算对象 |
| --- | --- |
| `prompts_hash` | 完整且有序的 `bundle["prompts"]` |
| `body_hash` | 准备阶段的规范正文记录摘要，直接使用 `bundle["body_hash"]` |
| `source_hash` | 角色 `source` 对象移除顶层本地 `path` 后的规范 JSON 摘要 |

使用准备阶段已物化并验证过的角色 bundle 调用 `research_protocol.role_execution_identity(bundle)`，可一次生成上述对象。文件位置不构成数据内容身份；正文、system prompt、逐题身份、顺序、来源及 revision 不能随着文件迁移被替换。

R1/R2 的 development 和 mechanism-development 必须保持相同的 `prompts_hash` 与 `body_hash`；fit/monitor 的两个 profile 可以按已注册映射保存不同内容。角色清单必须完整，与 `role_mappings` 对应，不能只冻结 prompt ID。

这些内容在 R1 前冻结。已经开始运行后再变更正文、评分器或精度，应另立目标和 campaign，重新取得适用的进展与资源证据。

## 新旧制品必须整条链同版

acceptance、candidate lock、reproduce 必须使用对应的同一研究版本。两方向混用均拒绝；新复现文件的 `execution_identity_hash`、`study_execution_hash` 必须为非空值并与候选锁完全一致。不能通过改一个 schema 标签把旧验收解释为新策略的证据。

## 阶段显存上限立即执行

每个实际测量阶段结束后、下一阶段重置 CUDA 峰值计数器前，检查该阶段峰值。超出冻结 allocated 上限的阶段标为失败并抛出运行异常；后续低峰值不能清除先前超限事实。嵌套阶段使用外层峰值，原始运行异常不会被资源检查覆盖。

CPU 桩回归只验证上述控制流与错误语义。实际模型的显存容量、耗时和行为效果仍须执行真实 R1/R2 测量。

## 预算目录和原生计费证据

R1、R2、search 和 experiment 的阶段账本使用冻结的绝对路径；不同阶段不得共用同一账本，不接受依赖启动目录的相对路径或父目录跳转。离线准备可验证 Linux 和 Windows 绝对路径形式，实际执行时路径必须适用于当前平台。

正式搜索和单卡实验在分配模型前，以目标契约、方法/策略/seed、study 身份和唯一运行目录原子登记成员。换目录不会得到另一份 24 次尝试和小时预算；原目录恢复使用原累计预算。已登记但缺失原预算或运行记录的目录不能被当作新运行重新领取。新的目标或明确注册的独立实验仍需自己的完整契约与放行证据。

资源预测同时验证来源文件、源协议、完整校准 profile、已注册 R2 调用、已结清计费区间和该调用的输出清单。账本由运行器写入 `source_protocol`、`session_index`、`evidence_files` 等绑定信息；成本汇总引用这些原生输出，不能靠手写同名阶段补齐测量。

阶段账本中必需的进展、资源和重放调用必须全部成功，不能用另行预注册的较窄探针替代失败的最宽资源调用。普通比较项的数值失败可以保留；比较项发生资源或身份错误则同样阻断放行。异常退出、硬超时及孤立 worker 收尾持久保存失败类别与原因，未完成调用不能被视为正常对照失败。

| 成本项 | 实际计费的原生阶段 |
| --- | --- |
| `load` | 原生模型加载计时 |
| `trial_bound` | 完整 `pilot` 调用，同时具备其昂贵分支测量及工作量证据 |
| `shortlist_bound` | 预注册资源项的完整 `shortlist`，倍率至少覆盖三个候选 |
| `replay_bound` | 两次完整 `replay-1`、`replay-2`，取最慢值 |
| `apply_reload_export_bound` | 完整 `third-apply`、`reload`、`export`，分阶段取最大值后求和 |

公式仍为 `1.25*(load+24*trial+shortlist+2*replay+apply_reload_export)`。成本项必须实际引用对应完整阶段；即使同一源文档含有完整 trial，也不能让 `trial_bound` 改用其中很短的 `capture` 子阶段。

纯效果失败、零更新基线和共享资源/身份故障使用不同状态。目标 seed42 全零会停止后续目标 seeds；合法基线的效果失败继续保留为对照。资源、身份以及必需重放/暂存故障写入 campaign 阻断记录，不能因 study 已结束就将其解释为完整成功。
