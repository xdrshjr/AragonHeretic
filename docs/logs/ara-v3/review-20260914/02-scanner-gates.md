# 阶段门禁与预算增量扫描

日期：2026-09-14。角色：code-diagnosis Scanner。契约：`docs/plans/ara-v2-refusal-optimization/spec.md` v3.2。本文是独立初扫，问题仍需 Reviewer / QA 复核；没有修改源代码、测试或 Git 索引，没有访问服务器或任务凭据。

范围：`ara_pilot.py`、`research_budget.py`、`ara_research_runner.py`、`pro6000_prepare.py`、`pro6000_experiment.py`、`pro6000_launch.py`、`ara_refinement_config.py` 及对应测试；为确认调用可达性，另只读查看 schema、协议、capture 及规格相关段落。

初扫结论：P0 = 0，P1 = 5，独立新增 P2 = 0。下述问题涉及资源证明真实性和累计预算，不能由已有工程测试通过替代。

## SCAN-GATE-001 — P1：完整成本项可引用任意短子阶段

- 类型：BUG / 放行真实性。
- 定位：`src/heretic/research_budget.py:427`、`:441`、`:498`（相关范围 427–454、498–522）。
- 触发：原生 trial 包含完整 `pilot` 耗时，也包含较短的 `capture` 等阶段。为 `trial_bound` 写 observation 时误选子阶段；其他成本项同理。
- 原因：`_measurement_seconds` 仅核对 observation 声明的 phase 与记录一致；`_validate_measured_branches` 检查的是整个 source 文档中递归搜到的阶段。被实际计费的 observation 无须覆盖该成本项所要求的阶段。因此整个 source 有 replay/export 阶段时，也能把所有成本项都指向同一条 capture 计时。
- 实际复现：直接调用当前 `pilot_cost_prediction`，使用与 `tests/test_ara_pilot.py::NativeResourceGateTests.test_native_phase_costs_use_frozen_formula_and_required_coverage` 相同结构的原生计时夹具。各完整阶段为 1000 秒、capture 为 0.001 秒；诚实引用完整阶段会报资源预算超限。保持 source 字节与 `covered_branches` 不变，只把五个成本项的 observation 全改为 `phase=capture, record_path=[measurements,capture]`，函数接受并产生 **0.03875 秒**预测。
- 影响：完整工作实际预测应超 8 小时，却可产生通过 search/experiment 门禁的资源预测。
- 最小回归断言：在上述现有测试中，把 `terms["trial_bound"]["observations"][0]` 改成 `measurements.capture`；断言 `PilotGateError`。对 `load`、`shortlist`、`replay`、`apply_reload_export` 分别执行相同负例；后两者还应验证被计费 observation 覆盖每种必需操作。
- 修复建议：给每类成本项规定实际 observation 的 phase 集合与记录类型。trial 只能使用完整 trial 调用计时，同时对该调用的嵌套昂贵分支作覆盖验证；replay 必须实际引用两次完整 replay，apply/reload/export 必须实际引用三类操作并分别计费。不能以 source 任意其他位置存在阶段名替代计费项覆盖。

## SCAN-GATE-002 — P1：成本来源未绑定目标契约、完整校准或共享 R2 账本

- 类型：BUG / 身份与预算。
- 定位：`src/heretic/research_budget.py:388`、`:458`（相关范围 458–475）；`src/heretic/ara_pilot.py:242`、`:245`。
- 触发：资源清单引用同 method/policy 的另一份 native 记录，例如其他模型/硬件 campaign、R1 smoke 或未在 R2 登记的额外运行；给引用提供其正确文件 hash。
- 原因：`_measurement_seconds(observation, member)` 仅检查 source 的自有 identity hash 和 method/policy。既没有 contract 参数，也没有检查 source protocol、目标 hash、profile/角色计数、硬件、stage_execution_id、预算调用状态或 evidence_hash。`build_pilot_readiness` 对资源预测与 R2 ledger 分别验证，两者没有关联。
- 证据：现有成本“可放行”测试只提供 `{method_id, proposal_policy}` 两字段的 execution identity，无 protocol、stage、角色或硬件，当前实现即可接受。这不是对签名真实性的额外要求，而是缺少契约中已规定的来源交叉绑定。
- 影响：旧 smoke、小模型或另一块更快 GPU 的成本可以被解释为本目标完整校准成本；未登记或未计费的资源补测也可进入放行证明。
- 最小回归断言：现有成本正例先扩展为绑定完整 target / R2 ledger 的真实结构；保持计时和 method/policy 不变，分别改 source 的 target、profile、hardware 或 stage_execution_id，并更新其合法文件 hash，断言拒绝。仅改变文件而不更新引用 hash，只能覆盖已有文件完整性保护，不能覆盖此问题。
- 修复建议：将 contract 与共享账本传入成本验证；从 observation 追溯完整 source protocol，验证目标身份、full-calibration 的角色身份和真实设备，再匹配预注册的 R2 调用、调用终态与输出 hash。所有资源源文件必须能由账本中实际计费调用定位；原生资源汇总应绑定其 source_trial。

## SCAN-GATE-003 — P1：相对账本路径导致换启动目录重新领取 R1/R2 预算

- 类型：BUG / 累计预算。
- 定位：`src/heretic/research_budget.py:87`、`:90`；`src/heretic/ara_refinement_config.py:323`（`validate_target_settings` 的 R1/R2 预算验证）；账本创建在 `research_budget.py:135`。
- 触发：目标契约采用合法的 `ledger_path="R1.json"`；在两个不同 CWD 启动同一个冻结 R1 调用。
- 原因：契约仅要求 ledger_path 非空，`phase_budget_path` 原样构造 Path。相对路径根据各进程 CWD 解析，进程锁也落到不同文件。
- 实际复现：从 `tests/test_ara_pilot.py` AST 提取原有 `contract_fixture()`，不改其内容。`validate_target_contract(contract)` 成功，其 R1 路径就是 `R1.json`。在临时 `cwd-a`、`cwd-b` 分别调用真实 `phase_budget_path` 并进入真实 `StudyBudget`，得到两个不同绝对路径，两个实例的历史 session 数均为 0。未启动模型、未等待计时器，也未模拟预算实现。
- 影响：同 campaign / stage 可以重复执行已登记调用、并行占用资源，并获得两份完整 8 小时预算。
- 最小回归断言：现有 contract fixture 的相对路径必须在冻结阶段被拒绝，或在固定 campaign 根目录被规范化；随后断言两种 CWD 返回同一个 canonical path、第二个实例看到同一历史调用并不能重跑。
- 修复建议：冻结时使用明确、不可变的 campaign 根目录，将所有账本路径规范化为绝对路径并把规范化结果纳入 target hash；运行时不再按 CWD 推导。验证 R1、R2 的路径互异且与预算身份一致。

## SCAN-GATE-004 — P1：正式同成员可换目录或并发重复获得完整 study 预算

- 类型：BUG / 并发与恢复。
- 定位：`src/heretic/research_budget.py:39`、`:61`、`:93`；`src/heretic/ara_research_runner.py:561`、`:584`；单卡 `src/heretic/pro6000_experiment.py:187`、`src/heretic/pro6000_launch.py:81`。
- 触发：同一冻结 protocol、method/policy/seed 使用不同 `--run-dir` 启动；也可以在首个 study 完成后重新指定另一个目录启动。
- 原因：`campaign_search_status(..., study=None)` 只读检查，不登记 RUNNING reservation，不绑定 study hash 和唯一运行目录；对 primary seed42 无条件返回。正式预算路径固定为各自 root/budget.json，worker/study 锁也只覆盖各自 root。终态写入只是覆盖 `members[member]`，无法阻止重复领取。单卡 `_run_search` 同样直接创建 `StudyBudget(root / "budget.json", run["hours"] * 3600, devices=1)`；launcher 每次生成含新时间戳和 UUID 的目录，同 target 的同 seed / experiment 成员没有共享领取记录，故可重复取得冻结的小时预算。
- 实际复现：连续调用两次真实 `campaign_search_status(protocol, primary_identity)` 均成功，且 matrix.json 仍不存在；随后真实 `phase_budget_path(..., "search", run-a/run-b)` 分别产生两个独立 budget.json。v3.1 `study_identity` 不含 run_dir，所以两者可保持相同逻辑 study 身份。
- 影响：同一预注册成员可产生超过 24 次尝试和超过 8 小时的累计成本，任取更优 study 覆盖 campaign 结果；两个进程也能同时执行相同成员。违反“已完成同身份 seed 不重跑/重复计费”的契约。
- 最小回归断言：在临时 campaign 中原子申请同一 member 的两份不同 root，第二份必须在模型分配前拒绝；已完成后再次申请不同 root 也必须拒绝或返回原制品。不同成员可按冻结顺序申请，正常原目录恢复复用同一累计预算。
- 修复建议：把正式 study 的唯一目录、study_execution_hash、RUNNING/终态和规范预算路径记录到 campaign 账本。锁内原子占位并返回预算定位；恢复校验同一 reservation，完成记录不可被同成员另一目录覆盖。单卡 experiment 若也宣称按目标成员限额，应采用相同身份去重或显式另行预注册独立 run。

## SCAN-GATE-005 — P1：资源故障被写为主矩阵 COMPLETE 并允许后续目标 seed

- 类型：BUG / 故障分类与停止规则。
- 定位：`src/heretic/research_budget.py:42`、`:49`、`:69`；`src/heretic/ara_research_runner.py:315`、`:614`。
- 触发：目标 seed42 的 trial 均因 CUDA OOM / 设备 RuntimeError 失败，或搜索完成后的候选重放出现身份/运行故障。
- 原因：`_run_attempt` 只对 ResearchBudgetExceeded、SnapshotRestoreError 立即抛出，普通 OOM/设备错误可形成 FAIL attempt；`campaign_search_status` 无条件把最终 study 记 `state=COMPLETE`。没有 COMPLETE trial 时，`all_zero_update=False`，所以后续 seed 可进入。另一个窗口是 campaign 在 replay/staging 之前已写入 COMPLETE；重放失败不会把 campaign 改为阻断态。
- 实际复现：给真实 `campaign_search_status` 一份 24 条 `FAIL / RuntimeError / CUDA out of memory` 的 target seed42 study；它生成 `state=COMPLETE, all_zero_update=False, effect_status=failed`，随后 seed43 的真实 admission 调用成功。
- 契约：规格第 555、641 行明确 OOM、身份或共享资源异常仍应阻断，不能套用对照零更新/效果失败豁免。
- 最小回归断言：扩展 `test_target_zero_update_stops_target_seeds_only`，分别加入资源失败的目标和基线 study，后续扩展须被阻断；纯零更新基线继续允许其他已获放行成员。再注入 replay 错误，确认 campaign 不遗留 COMPLETE 放行状态。
- 修复建议：区分工程/资源终态与效果终态，保留阻断原因和未运行清单；仅成功完成必需 replay/staging 后提交最终 COMPLETE。资源/身份失败应在 campaign 层锁存并阻断后续 admission；不能仅以是否有非零 COMPLETE trial 判断运行健康。

## 已核查且未另列问题

- R1/R2 在使用同一个绝对账本路径时，`run_budgeted_phase` 用共享文件锁覆盖预算恢复、模型分配和调用执行；完成项可在耗尽预算时只读返回，失败调用不能直接重试。问题集中在定位身份及跨成员生命周期，不能笼统认定“完全没有锁”。
- `validate_phase_readiness` 对 full-calibration pilot 要求 active_update；新 Pro 6000 launcher/prepare/worker 均有 readiness 校验，缺失证明可在模型加载前拒绝。未发现需要另列的“入口完全漏校验”。
- `validate_experiment_budget` 核对实际 seed、小时数与单卡设备，不能直接借用更宽预算；仍受上列成本来源问题影响。
- `RefinementConfig` 对固定 alpha 序列、旧求解器策略以及旧 schema 新字段有明确约束；本范围没有发现额外配置 P0/P1。
- `pro6000_prepare.py`、`pro6000_experiment.py` 的基线差值和独立实验声明保持 comparison_only / research not_run 边界；未把单纯 completed 单独报告为正式研究通过。
- 正式零更新目标停止与基线继续的正常分支有现有单元用例；缺失的是资源故障、跨目录重复申请和成本 observation 交叉绑定负例。

## 验证边界

本地 Python：`D:/Tools/Anaconda/python.exe`，没有安装 torch。以上复现直接运行当前 schema/config/budget/runner 的可导入纯逻辑，只用临时文件夹和原生函数；没有使用伪 torch 运行数值断言。没有运行完整模型测试、GPU 实验或更改现有测试。这些结果证明控制流与预算问题，不证明模型效果。具体复现输出已发送主代理，可用现有 fixture 加入修复前失败 / 修复后成功回归。
