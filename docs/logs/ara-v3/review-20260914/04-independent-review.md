# 独立 Reviewer 复核报告

日期：2026-09-14。角色：code-diagnosis 独立 Reviewer。输入为本轮三个 Scanner 报告，契约为 `docs/plans/ara-v2-refusal-optimization/spec.md` v3.2。下述裁定针对复核时读取的实现，尚待独立 QA 最终确认及修复后验收。

逐项重新读取了真实源码、规格和关联调用端；没有将 Scanner 的结论直接作为裁定依据。本阶段只读源码，没有修复、运行项目代码、访问远端、派生代理、修改 Git 索引或操作任务平台。Scanner 的离线复现可作为交叉证据，本文没有将其重复计为 Reviewer 实际运行的测试，也不宣称 GPU 或真实模型效果已经验证。

## 裁定总表

10 项全部具有可达的源码依据：8 项 CONFIRMED，2 项 MODIFIED，0 项 DISPUTED。P0 为 0，P1 为 8，P2 为 2。MODIFIED 表示问题成立但收窄表述或修复边界，不表示已经修复。

| ID | Reviewer 状态 | 级别 | 裁定 |
| --- | --- | --- | --- |
| NUM-001 | CONFIRMED | P1 | 顶层阶段重置 CUDA 峰值，历史超限可在 guard 前消失 |
| NUM-002 | CONFIRMED | P2 | 回溯异常恢复模型，但丢失已经计算的失败事件 |
| SCAN-GATE-001 | CONFIRMED | P1 | 完整成本项可计费短子阶段，实际计费与覆盖证明分离 |
| SCAN-GATE-002 | CONFIRMED | P1 | 成本来源未绑定目标、完整校准和已计费 R2 调用 |
| SCAN-GATE-003 | CONFIRMED | P1 | 相对账本路径使同一冻结阶段在不同 CWD 获得独立总账 |
| SCAN-GATE-004 | MODIFIED | P1 | 同目标同成员可换目录重复领取正式 study 预算；独立新目标不应一并封禁 |
| SCAN-GATE-005 | MODIFIED | P1 | 运行故障/重放失败未阻断矩阵扩展；不能把普通效果失败一并当运行故障 |
| IDENTITY-001 | CONFIRMED | P1 | 目标未冻结实际 scorer、精度及共同 development 正文身份 |
| IDENTITY-002 | CONFIRMED | P1 | 旧 acceptance 可绑定新 reproduce，新增执行摘要未受对应版本验证 |
| IDENTITY-003 | CONFIRMED | P2 | 机制双侧 bootstrap 缺退化和重采样身份；当前无正式调用路径 |

## NUM-001：阶段峰值重置导致显存上限漏检

**CONFIRMED / P1。**

- 源码：`ara_refinement_capture.py:458` 的 `measure_research_phase` 在 depth=0 时于 474 行重置峰值，结束时只把峰值写入 `records`；`research_resource_status` 在 516–520 行仅检查 CUDA 当前计数器。
- 可达性：`_ResearchSession.apply`（`ara_research_runner.py:465`）直接调用 `apply_refinement_trial`，后者直接进入 `_run_sweeps → _block_transaction`，没有包住整个 trial 的外层测量上下文。`ara_refinement.py:314–325` 的 monitor_before、capture、local_optimization 因而是连续的顶层阶段；capture 结束与下一阶段重置之间没有预算/资源检查。
- 确切触发：capture 峰值超过冻结上限，临时张量释放，local_optimization 开始时重置计数器。其后 `_attempt` 或组结束的 `check_budget` 只能看到新阶段较小的峰值。`phase_resources` 已记录超限，但没有被 guard 读取。
- 排除误报：嵌套阶段的 depth 保护确实存在，但真实 trial 外层没有使这些阶段全部嵌套，因此不能消除此路径。不能把 AST 隔离复现直接说成 GPU 实测；成立依据是完整真实调用链。
- 最小修复：重置前核验上一阶段峰值，或维护独立于 CUDA reset 的每设备高水位并让资源 guard 使用它。失败仍应恢复完整 trial，不能作为普通 alpha 拒绝继续。
- 验收：连续两个真实测量上下文，第一阶段超限、第二阶段下降，仍抛资源错误；合法嵌套阶段及旧入口继续正常工作。

## NUM-002：异常丢失逐次失败证据

**CONFIRMED / P2。**

- 源码：`ara_backtracking.py:258–270` 的 finally 会恢复并写入局部 row，但异常绕过 return；`evaluate_block_proposals:287–288` 只在正常返回后 append。`ara_refinement.py:428–436` 同样只接收正常返回的 event；`apply_refinement_trial:471–497` 仅在成功时保存 trial.json。`ara_research_runner.py:318–319` 的 FAIL 行只保存异常类别和字符串。
- 可达性：真实输出统计已写入 row 后，monitor 抛错或第二次预算检查失败。当前 row、该组事件以及此前已完成组的 events 不会随异常返回到 trial 边界。
- 规格：269 行规定异常终止和恢复，547 行要求逐次原因、guard、误差和成本证据。本实现满足恢复的主要路径，但没有持久保留已计算的这些证据。
- 严重度理由：未发现此问题造成错误接受或快照恢复失败；损失的是诊断和审计可追溯性，因此维持 P2。
- 后续：预先登记 row/event，在异常边界保存 FAIL 证据并重新抛出原异常。验证恢复正确与证据保留两件事，不应只断言恢复。

## SCAN-GATE-001：成本计费和必经阶段覆盖脱节

**CONFIRMED / P1。**

- 源码：`research_budget.py:427–454` 从 observation 取计时；`_measurement_seconds:458–475` 仅要求选中记录 phase 与 observation 自述一致；`_validate_measured_branches:501–522` 却扫描 source 整体的递归阶段名。
- 可达性：`build_pilot_readiness:243–246 → pilot_cost_prediction → _measured_cost`。原生 source 含完整 pilot、capture、shortlist、重放和导出时，所有成本项均可引用其中很短的 capture 记录，且仍通过 source 级阶段覆盖检查。
- 规格：604 行冻结的公式需要完整 trial、三个 shortlist、两次 replay 以及第三次 apply/reload/export 的成本，不能由同源但未被计费的其他阶段名替代。
- 排除误报：基于观测最大值和工作量倍率的外推是设计允许的，不要求预测成为严格最坏上界；问题是所乘的观测根本不是目标成本项。现有倍率校验不补救选错基础阶段。
- 最小修复：成本项限定实际引用 phase；trial 引用完整 trial 调用，同时核验该调用的昂贵分支；replay 实际引用两次完整 replay；apply/reload/export 必须分别引用并计费对应操作。不得只在 source 任意位置找名字。
- 验收：保留 source 原始字节和 covered_branches，只把每个成本项的引用改为 capture，均须拒绝。正例仍使用原冻结公式及最大值外推。

## SCAN-GATE-002：成本来源缺少身份和 R2 计费绑定

**CONFIRMED / P1。**

- 源码：`pilot_cost_prediction:388` 未把 contract 传入 `_measured_cost`；`_measurement_seconds:462–467` 仅验证 execution identity 自身摘要及 method/policy。`ara_pilot.py:243–250` 分别核验资源预测与 stage ledger，没有将每条 observation 与 ledger call 关联。
- 可达性：同 method/policy 的其他 native 文件，只要正确更新文件摘要、自有 execution 摘要和阶段计时，即可供 search_readiness 计费；目标、来源 protocol、full-calibration、stage_execution_id、真实设备和调用终态均未在成本读取链检查。
- 规格：500/502/549/602/604 行要求目标环境、完整样本、预注册 R2 测量及同阶段累计预算。资源 source 的自洽摘要只能证明文件内部一致，不能证明属于本目标已计费的测量。
- 最小修复：把 target 和 R2 ledger 传入资源校验，从 source/source_trial 追溯真实来源协议、执行项与输出，验证完整校准身份、硬件、预注册调用、终态及 evidence_hash，并证明该调用已在同一 R2 总账计费。
- 允许边界：R2 seed42 的已登记资源测量可用于其他预注册目标 seeds 的资源外推；不能要求每个目标 seed 都已有实测。pilot 来源 protocol 与正式目标 protocol 本来不同，不应要求两个总 hash 相同；应核验共同 target 和明确 profile 映射。
- 验收：完整合法正例基础上分别改变 target、profile、设备或 stage_execution_id，并重新计算正确文件摘要，仍须拒绝；仅破坏文件 hash 不能覆盖本漏洞。

## SCAN-GATE-003：相对路径重复分配阶段总账

**CONFIRMED / P1。**

- 源码：`ara_refinement_config.py:323–331` 仅要求 ledger_path 非空；`research_budget.py:87–90` 直接 `Path(budget["ledger_path"])`；`run_budgeted_phase:293–303` 在该路径旁落锁并创建 StudyBudget。
- 可达性：同一冻结 contract 的 `R1.json` 在不同启动 CWD 解析成两个文件。两份进程锁互不干涉，StageExecution 的去重和历史累计都只存在于各自空账本中。
- 规格：483/602/639 行明确换目录仍累计同一 campaign/stage 总账。因此虽然生产模板可能习惯使用绝对路径，公开契约校验允许的相对路径仍是可达问题。
- 最小修复：以冻结且唯一的 campaign 根定位，或要求可在执行平台解析的绝对规范路径，并在目标摘要中固定定位结果；校验 R1/R2 不共用同一物理账本。冻结之后不应依赖调用进程 CWD。
- 验收：相同契约在不同 CWD 得到同一账本，或在冻结时稳定拒绝相对路径。第二次调用必须看见第一次的历史状态，不能再领取完整上限。

## SCAN-GATE-004：正式同成员可换目录重复领取预算

**MODIFIED / P1。保留问题，限定为同目标契约的同一预注册成员。**

- 源码：`campaign_search_status:39–41` admission 不落 reservation；`_check_target_admission:65–66` 对主成员直接返回；`:46–51` 完成记录可被覆盖。`phase_budget_path:92–93` 为每个 root 创建独立 budget.json；research runner 的 worker/study 锁也仅属于该 root。
- 可达性：`run_from_settings --phase search --run-dir A/B` 使用同 protocol/method/policy/seed 可通过两次 admission，随后创建独立 study 和完整预算。完成 A 后使用 B 也没有成员去重。`pro6000_launch.py:81–99` 每次生成新目录，而 `pro6000_experiment.py:179–190` 同样从当前 run 根创建独立预算。
- 规格：600 行规定 R3 已完成同身份 seed 不重跑、不重复计费；483/489 行将正式成员和 study 身份独立于目录定位。
- 修改 Scanner 表述：单卡是独立 experiment scope，不能把它的全部历史运行都等同于正式矩阵成员。应限制同 target 的同一 experiment 预注册成员重复领取；明确新增目标契约或另行冻结的独立研究可以有自身预算。也不要求不同合法成员共享同一 study 的 24 次次数。
- 最小修复：campaign 锁内原子保留成员与 canonical run_dir、study execution hash、预算路径及状态；同成员第二目录拒绝或只读返回原制品。同原目录恢复沿用原账本，失败/运行中状态不能被任意另目录覆盖。
- 验收：并发 A/B、完成后 B、同 A 恢复三种路径分别验证；新合法成员和显式新目标仍可执行。需在分配模型前完成占位和检查。

## SCAN-GATE-005：工程失败被当成完成矩阵成员

**MODIFIED / P1。问题成立；修复须按故障类型区分，不能把全部 FAIL 一刀切。**

- 源码：`_run_attempt:315–322` 可将资源 RuntimeError 记成 FAIL 后继续；`campaign_search_status:42–51` 对无 COMPLETE trial 的 study 仍写 state=COMPLETE、all_zero_update=False。后续 admission 只看主成员存在/全零。`_finish_search:614–623` 在 replay/staging 前先写 COMPLETE，后续重放/身份错误没有回写 campaign 阻断状态。
- 可达性：资源错误导致全部 trial FAIL，搜索形成 unavailable/effect failed，但 campaign 状态仍为 COMPLETE，允许后续目标 seed。另一直接窗口是候选写入 campaign 后 replay 抛错，其他成员仍能入场。
- 规格：555/641 行明确共享资源和身份错误不能借用基线效果失败的豁免。259/269 行同时说明 attempt 级异常须终止并恢复；不能以“已经结束循环”替代工程健康和后续放行判定。
- 修改 Scanner 表述：纯关键词/语义不达标、正常零更新的 B1/B2/S1 必须保留 comparison_only/unavailable 且不阻断其他获准成员。已有正式规则允许有限数量失败 attempt，不能仅凭任一 FAIL 或任一 RuntimeError 就推断 campaign 共享资源故障。必须保留可判定的故障类别与停止原因。
- 最小修复：为资源/身份等阻断故障持久登记 campaign 状态；必需 replay/staging 完成后才发布完成状态，出错则留下不可被其他目录绕过的失败状态与 not_run 原因。恢复不能重复消费已完成操作。
- 验收：全资源失败、共享设备/身份错误、重放失败必须阻断；正常基线全零和纯效果失败仍允许其他已获放行成员。目标全零继续只阻断后续目标 seeds。

## IDENTITY-001：目标契约遗漏真实执行条件

**CONFIRMED / P1。**

- 源码：`ara_research_schema.py:172–195` 的严格 target 字段没有 quantization/dtype；`ara_refinement_config.py:306–315` 只检查 scorer_identity 非空。`research_protocol.py:385–405` 与目标比较的字段不含实际 judge_identity，角色映射仅比较 prompt_id。
- 可达性：公开 `build_research_manifest → _protocol_payload → _validate_new_protocol` 接受各自 hash 正确、共同 target 相同但 judge、精度或 development 正文不同的协议。`prepare_protocol` 再核对的是本次 settings 与本次 protocol；`RoleEvaluator` 和语义评估实际读取 protocol.judge_identity。因此新协议内部自洽，仍可漂离原 readiness 的执行条件。
- 规格：215 行要求共同 development 的 ID、顺序、正文 hash 和评分配置完全一致；500/502 行要求 scorer、环境和共同开发条件随目标冻结。新 protocol hash 不同不是错误本身，但不能在固定 target 下任意改变这些条件。
- 最小修复：target 固定精度/量化、实际 scorer 配置，以及各 profile 的角色来源/正文/system 摘要，准备及运行前核对；明确 scorer_identity 与实际 judge_identity 的对应关系。readiness 来源也应验证同一目标下的这些条件。
- 排除误报：R1 fit/monitor 与 R2 的合法数量和 ID 映射差异应保留；不同 trial 的 adapter 身份必须不同。不能为了补正文校验要求整个 ScoreIdentity 或整个 protocol hash 跨 profile 相同。
- 验收：保持 target 不变，分别变动 scorer 参数、精度、development text/system 并重新计算协议/正文摘要，应拒绝；合法冻结的 smoke→full-calibration 映射应通过。

## IDENTITY-002：acceptance/reproduce 跨代绑定不对称

**CONFIRMED / P1。**

- 源码：`ara_research_acceptance.py:48–53` 仅在 acceptance 为 v3.1 时比较 reproduce 版本和执行 hash。旧 acceptance + 新 reproduce 不进入此分支。`ara_research_schema.py:575–588` 要求新 hash 字段存在，却没有加入非空字段循环。
- 可达性：`load_bound_acceptance → validate_acceptance_reproduce_binding → validate_research_binding`，以及 `verify_research_artifact_graph` 都使用此函数。提供合法旧 report/core/候选锁，并在 reproduce 使用 v3.1 schema 和任意新执行 hash，现有绑定检查无法拒绝。
- 规格：494/559/642 行要求精确新旧分派，不把旧制品补字段解释为新身份。旧 hash 保持正确反而使普通完整性校验不会发现此语义混用。
- 最小修复：先无条件检查 report/reproduce/candidate 所属代次一致，再按代次核验内容；新 execution/study hash 至少非空并绑定对应候选。旧合法 v3 路径保持原有语义及原始文件字节。
- 验收：覆盖两个交叉方向、空新执行 hash、正确同代旧/新正例，并通过通用文件加载入口验证。不能只测新报告中改一个 hash。

## IDENTITY-003：双侧机制 bootstrap 未覆盖新契约

**CONFIRMED / P2。**

- 源码：`research_evaluation.py:249–268` 仅检查至少两组；组差值全相同时仍返回零宽区间。函数允许自由 seed，结果未保存逐组配对值、抽样身份或分析类型。
- 规格：331 行明确同时约束机制双侧和语义单侧分析：固定 seed/抽样次数/分位数，退化 inconclusive，保存可核验身份。
- 可达性与级别：该辅助函数直接调用可返回错误的退化分析状态，但源码搜索未见正式执行路径调用它。新语义单侧函数另有退化处理，不能把本项扩大为“正式语义分析已被错误放行”。因此保持 P2。
- 后续：通过明确新分析版本补充退化判断及重采样身份，保留历史报告字节；新增机制调用端在发表机制结论前必须使用合规路径。

## 修复和 QA 的交叉约束

1. NUM-001 与 SCAN-GATE-005 应共同验证：检测到资源超限既要恢复 trial，也要保持正确的运行故障分类，不能只是把异常转换为普通效果失败。
2. IDENTITY-001 与 SCAN-GATE-002 应采用同一目标身份解释：共同 development、实际 scorer/精度/硬件严格绑定，合法 profile/seed 映射继续允许。
3. SCAN-GATE-003/004 修复后同时测试同目录恢复与不同目录重复申请，防止去重修复意外关闭合法恢复。原子锁须覆盖占位及预算定位。
4. P0/P1 修复完成前不能给代码评审通过结论；两个 P2 可列待办，但对应失败诊断或机制分析的科学声明仍受证据限制。

Reviewer 未发现需要追加的独立 P0，未将尚未运行 GPU、尚未形成论文效果或合法失败基线作为代码缺陷。本文没有替代最终 QA 裁定。
