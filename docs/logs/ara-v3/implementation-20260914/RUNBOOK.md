# v3.1 运行与证据接口说明

本手册对应规格 v3.2 的第二轮代码实现。以下命令说明输入格式和执行顺序；路径由执行者替换为真实冻结文件。本目录没有新的 GPU pilot、readiness 或正式研究成功证据。测试夹具中的模型身份、问题 ID、计时均为人工数据，不能用于真实放行。

## 先冻结目标契约

目标文件使用 `cara-target-execution-v1`，通过 `validate_target_contract()` 校验，规范 JSON 摘要由 `digest()` 计算。顶层字段严格为：

```text
schema_version campaign_id model_identity tokenizer_identity
initialization_sources source_files package_versions members seeds
parameter_ranges initialization_scheme role_layout role_mappings
generation_profiles scorer_identity hardware stage_executions
phase_budgets validation_rules
```

`seeds` 固定 `[42,43,44]`，`initialization_scheme` 固定 `paired-data-v3.1`，参数范围来自 `PARAMETER_RANGES`。`members` 使用完整的 `method/policy/seed`，例如 `S2/spectral-backtrack-v1/42`；目录使用 `member_identity()` 生成的存储键。源码、模型、tokenizer、数据来源、评分器及依赖版本必须是真实身份。`hardware.devices` 为实际 1 或 2，`hardware.device_names` 与运行时逐卡名称一致。

契约不能包含自己的摘要、输出路径、readiness、试点结果或后代协议摘要。`source_files` 冻结稳定源码；单卡入口生成的 runtime TOML 不放入祖先契约，避免循环依赖。后代协议引用目标契约及摘要，运行时继续核验模型、量化、生成配置和真实来源。R1 开始后不得修改目标，再加方法、设备或预算需要另立 campaign。

`role_layout` 使用 `pilot-preserving-v1`；`role_mappings.smoke` 和 `role_mappings.full-calibration` 分别列出全部角色的有序 prompt ID。

| profile | 每侧 fit 候选/选用 | 每侧 monitor | 每侧 development |
| --- | --- | --- | --- |
| smoke | 8 / 8 | 8 | 100 |
| full-calibration | 192 / 96 | 64 | 100 |

完整布局保留原 development 的 100 条身份与顺序。每个 profile 还须保存 `_fit_selected_ids`，由以下接口一次生成并冻结，运行时会重算核对：

```python
from heretic.ara_refinement_config import freeze_fit_ids

for profile, mapping in contract["role_mappings"].items():
    mapping["_fit_selected_ids"] = freeze_fit_ids(mapping, profile)
```

该值按字符串 seed `42`、`43`、`44` 映射到 `good`/`bad` 的选用 ID；抽样采用 `digest([seed,"fit",side])` 派生种子，选中位置排序后保留候选顺序。

`validation_rules` 固定 rank=128、max_singular_value=8、max_cumulative_ratio=0.60、relative_change=1e-6，以及 `[1,0.5,0.25,0.125,0.0625]`。部署谱上限另乘 `1-1e-6` 裕量。

## 预注册阶段工作及共享预算

每个 `stage_executions` 项使用 `StageExecution` 字段：campaign_id、stage、stage_execution_id、member_id、profile、purpose、parameters、parameter_hash、initialization_attempt、operation、parent_stage_execution_id、budget_key。参数 hash 来自完整参数 JSON；原始调用的 operation 为 `trial`。固定项如下：

| stage_execution_id | 策略/参数 | purpose | profile |
| --- | --- | --- | --- |
| r1-reject-anchor0 | reject-v1 / anchor0 | comparison | smoke |
| r1-scale-anchor0 | scale-v1 / anchor0 | comparison | smoke |
| r1-clip-anchor0 | spectral-clip-v1 / anchor0 | comparison | smoke |
| r1-backtrack-anchor0 | spectral-backtrack-v1 / anchor0 | progress | smoke |
| r1-reject-quarter-anchor0 | reject-v1 / anchor0 两 strength 乘 0.25 | comparison | smoke |
| r2-backtrack-anchor0 | spectral-backtrack-v1 / anchor0 | progress | full-calibration |
| r2-backtrack-anchor2-resource | spectral-backtrack-v1 / anchor2 | resource | full-calibration |

以上成员均为 S2、seed 42。anchor 值使用 `paired_parameters(42,0)` 或 `paired_parameters(42,2)`，不能重新调参。R1 进展项另注册 `reload`；R2 进展项另注册 `replay-1`、`replay-2`、`third-apply`、`reload`。子项 purpose 为 `replay`，继承父项参数、initialization_attempt、成员和阶段，每个操作单独执行、计费及保存证据。reload 必须来自独立进程，使用无审计探针。

`phase_budgets.R1` 和 `.R2` 各自包含 `max_wallclock_seconds=28800`、实际 devices、`max_gpu_hours=8*devices`，以及唯一的绝对 `ledger_path`。同阶段所有调用共用该账本，换 run-dir 不会获得新的 8 小时。全部预注册调用必须收尾；失败对照可以记录 FAIL，但不能省掉调用或临时增加进展项。

扩大搜索另冻结 `phase_budgets.search.ledger_path` 和 `members.<method/policy/seed>` 的设备、墙钟与 GPU-hours；单卡入口使用 `phase_budgets.experiment.members`，每个实际 seed 的预算必须精确等于本次 `--hours` 和 devices=1。若同时注册多个用途，资源预测取同成员所有预算中的严格上限。

B1、B2、S1 及其他目标成员的资源工作、重放和重载也必须在 R1 之前注册。纯资源项调用真实 semantic/shortlist 与 comparison export，可用于测成本，永久不参与挑选进展候选。准备这些调用前必须具备冻结的评分器输入。

## 准备协议与运行 R1/R2

独立准备 JSON 继续使用现有来源、许可、revision、角色与能力清单接口；新版增加 `schema_version="cara-research-protocol-v3.1"`、目标契约、role_layout 和 pilot_profile。运行：

```bash
python scripts/prepare_ara_research_protocol.py --config /path/preparation.json
```

从模板为每个预注册调用另存配置，并绑定它自己的协议与 stage_execution_id；不要覆盖旧运行的冻结 TOML。关键配置为：

```toml
[ara_v3]
artifact_schema = "cara-research-acceptance-v3.1"
proposal_policy = "spectral-backtrack-v1"
backtracking_alphas = [1.0, 0.5, 0.25, 0.125, 0.0625]
spectral_projection_margin = 0.000001
pilot_profile = "smoke"
stage_execution_id = "r1-backtrack-anchor0"
protocol_manifest = "/path/r1-protocol.json"
```

这只是配置节示例，仍需模板中的模型、数据、设备和其他参数。对照配置使用对应策略；R2 改为 full-calibration。调用示例：

```bash
bash scripts/run_ara_research_96.sh \
  --config /path/r1-backtrack.toml \
  --run-dir /path/campaign/r1-backtrack --phase pilot
```

每个注册项只执行一次。运行目录保存原生 trial、因子快照和阶段结果；进展原项生成 `pilot_only` 锁。重放/重载通过父执行项关联原始 trial，不另挑参数，也不提升试点锁。已完成结果恢复会先校验文件及快照，只读返回；孤立 RUNNING 计入 FAIL/interrupted 并保守结账。

## 离线生成 readiness

入口只读取文件，不加载模型：

```bash
python -m heretic.ara_pilot \
  --contract /path/target.json \
  --evidence /path/readiness-input.json \
  --output /path/active-update.json
```

readiness 输入包含 `stage`、`target_execution_hash`，以及 `protocol`、`trial`、`lock`、`reload`、`stage_ledger` 文件引用。引用格式为 `{"path":"绝对路径","sha256":"文件原始字节摘要"}`，可调用 `bound_record(path)` 生成。后两级还需按顺序提供三项 `replays`；search_readiness 另需 `resource` 引用。reload 的因子一致性、独立 PID 和探针分数均会重算核对。

| stage | 必须证明的内容 | 使用位置 |
| --- | --- | --- |
| active_update | R1 anchor0 至少一组接受、实际有效 BA 改变、重载一致及阶段预算完整 | R2 协议的 pilot.readiness_evidence |
| development_progress | R2 全布局、固定 100 条开发集至少少 5 条关键词拒答、两个 KL 均 ≤0.15、两次重放与第三次 apply/reload | 进展证据 |
| search_readiness | 上述 R2 进展加每个目标成员的真实成本证明 | search/experiment 放行 |

协议在 `phase_budgets.<phase>.readiness_evidence` 引用结果，启动时重建结论并逐字段比较。旧零更新摘要、手写 passed、缺分支计时或修改源文件都不能放行。纯代码测试无法生成真实 readiness。

## 成本证据

资源文件的 `members` 必须恰好覆盖目标 search/experiment 成员；每成员有且仅有 `load`、`trial_bound`、`shortlist_bound`、`replay_bound`、`apply_reload_export_bound` 五项。每项包含 `observations`、`workload_multiplier`、非空 `workload` 和 `covered_branches`。一次 observation 形如：

```json
{
  "source": {"path": "/真实原生文件.json", "sha256": "真实摘要"},
  "record_path": ["phase_measurements", 0],
  "phase": "load"
}
```

record_path 必须指向该原生文件中实际存在、status=complete、计时为正的对应记录；上例的数组位置仅示意，不能假设每个 trial 都在同一位置。来源文件绑定 execution_identity 及其摘要，方法/策略不能借用其他求解器。缺测不能填零。

trial 需实测 capture、local_optimization、keywords、prefix_log_odds、sequence_kl；S1/S2 另需 actual_forward、monitor_after。还要有 shortlist、replay-1、replay-2、third-apply、reload、export。原生 `workload_evidence` 包含 target_modules、observed_modules、sweeps、max_backtracks；外推至少覆盖模块比、目标轮次比及五次回溯比。shortlist 倍率至少 3。apply/reload/export 分阶段取最大值后相加，其余项取实测最大值乘工作量倍率。

预测式固定为 `1.25*(load+24*trial+shortlist+2*replay+apply_reload_export)`，同时满足墙钟和实际设备 GPU-hours。嵌套测量不重置外层峰值；`peak_scope=enclosing_phase` 的 GPU 峰值是包含外层工作的保守上界，不能宣称是该子操作独立峰值。异常会话的 `stop_estimated` 也不能解释为精确 GPU 忙时。

## 扩大运行、语义比较与正式制品

取得 search_readiness 后，双卡入口以 `--phase search` 运行已冻结成员。目标 S2/spectral-backtrack-v1 首先运行 seed 42；若其 24 次全部零更新，seed 43/44 记录 not_run。基线失败不触发这条目标停止规则。

单卡新运行示例：

```bash
bash scripts/run_qwen38_27b_ara_v3_pro6000.sh \
  --target-execution-contract /path/target.json \
  --readiness-evidence /path/search-readiness.json \
  --seed 42 --hours 48
```

48 小时只是示例，必须与该 seed 的 experiment 预算完全一致。单卡 launcher 使用 Git 提交归档源码；本节点未提交，真实启动应使用下游评审并提交后的版本。旧运行通过原 resume 入口恢复，仍读取旧身份，不能顺便升级策略或 schema。

comparison 导出分别记录 improved/no_improvement/inconclusive 和 research_status；工程退出 0、成功写出 adapter 都不等于研究 passed。正式通过仍要求完整 acceptance/reproduce/audit 制品图。新 recovery 通过使用 `recovery_supported`，不冒充达到极低拒答率。

语义比较要求 B0 和对应基线 B1/B2/S1 的冻结原始响应、组 ID 及双评结果；固定 10,000 次配对重采样、seed=20260910、candidate-minus-baseline 的单侧 95% 上界。退化、缺成员或身份不一致为 inconclusive。三个 seed 分别比较，全部通过才给方法级 superiority；它与绝对拒答门槛分开报告。

退出码 2 表示配置/身份错误，3 表示执行或预算失败，4 表示效果门槛失败，5 表示证据不足；wrapper 透传真实退出码。真实 27B NF4 GPU 跑通、资源测量、完整搜索、人工语义双评、正式审计和跨模型结论仍待执行。
