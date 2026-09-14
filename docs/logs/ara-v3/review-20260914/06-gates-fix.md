# 门禁与预算修复交接

日期：2026-09-14。对应 Scanner 的 GATE-001 至 GATE-005；源码修复已完成，最终独立 QA 由主代理组织。

## 修复内容

1. 成本项严格验证实际计费 observation 的完整阶段。trial 不再允许引用 capture 等子阶段；两次 replay 和 apply/reload/export 各阶段必须实际计费，完整昂贵分支仍独立检查。
2. 成本来源追溯到目标契约下已结清的 R2 共享账本：核对完整预注册执行、实际预约区间、source_protocol、trial/resource 文件引用、原始 trial 输出 hash、参数/初始化身份和 full-calibration 完整角色计数。原生 pilot 执行同步登记这些字段；reload 补齐原始协议和执行身份。离线 readiness 也复用完整新版协议校验。
3. R1/R2 及新 search/experiment 的账本路径必须为预先冻结的绝对路径，允许验证 Linux 或 Windows 的绝对路径表达；实际运行拒绝不属于当前平台的路径。各阶段路径不能相同。
4. 正式及单卡成员在模型分配前原子登记唯一 study、运行目录和预算文件。同目录恢复复用原预算；其他目录不能再次领取相同 target/member。已登记预算丢失即拒绝；study 开始后记录其路径，丢失 study 也拒绝重新领取尝试次数。旧 v3 不进入此新登记流程。
5. 普通数值求解失败仍走原健康计数；资源、身份、重放或暂存故障持久阻断 campaign。完成状态移到必需重放/暂存之后。watchdog 直接退出时，下一次准入从已登记预算目录读取硬超时标记，并持久写入阻断状态；非主成员的超时同样阻断。

首次预算文件在写成员占位之前创建，因此“占位已存在但没有预算文件”始终按缺失证据拒绝。首次模型加载中断但 study 尚未开始时，同目录仍可累计恢复；已经开始的 study 不能通过删文件清零。未修改旧 v3 的恢复与预算行为。

## 新增回归

- `NativeResourceGateTests.test_complete_cost_cannot_charge_an_unrelated_subphase`
- `NativeResourceGateTests.test_relative_stage_ledger_is_rejected_but_linux_absolute_is_valid`
- `NativeResourceGateTests.test_cost_source_requires_registered_complete_r2_call`
- `NativeResourceGateTests.test_cost_source_rejects_self_consistent_protocol_drift`
- `PilotTests.test_readiness_rechecks_self_consistent_source_precision_and_scorer`
- `RunnerTests.test_runtime_failed_primary_blocks_following_seed`
- `RunnerTests.test_campaign_reservation_rejects_duplicate_directory_and_keeps_budget`
- `RunnerTests.test_started_member_cannot_reset_attempts_by_removing_study`
- `RunnerTests.test_replay_failure_latches_campaign_but_numeric_failure_does_not`
- `RunnerTests.test_nonprimary_hard_timeout_blocks_other_campaign_members`
- `PreparationTests.test_single_card_member_cannot_claim_another_run_budget`

原成本正例改用完整 R2 协议、独立调用和实际计费区间夹具；保留 41.25 秒的固定公式结果。旧“缺昂贵分支”负例最后补上合法 phase，确保它检查真正的分支缺失。成本协议漂移负例使用正则限定目标语义错误，避免仅因陈旧 trial 引用而误通过。

## 已执行验证及证据

- 修复前首批五项：3 个断言失败、2 个新接口缺失错误；保留的终端摘要见 [红色回归记录](gates-before.log)。另两处成本来源缺失的初始可达复现见原扫描报告及工具调用。
- 本地无 torch 的真实纯逻辑验证：16 项通过，0.500 秒，见 [绿色回归原始输出](gates-after.log)。测试不伪造 torch 模块，不执行数值或模型验证。
- Ruff 默认检查、显式 80 列 E501、C901≤10、相关路径 `git diff --check` 通过。五个源码模块均不超过 800 行、函数不超过 50 行和 5 个业务参数；无新增模块。最后 source 行数：research_budget 774、ara_pilot 751、ara_research_runner 764、ara_refinement_config 569、pro6000_experiment 366。
- 主代理通知：24 模块共 270 项通过，7 项入口检查和 1 项真实子进程检查通过，项目增量静态审计通过。该结果由主代理执行；本代理未访问远端。主代理将对最后一处“缺昂贵分支”夹具精化补跑资源测试组。

## 修改边界

仅修改获准的五个源码文件及三个测试文件；没有改 `pro6000_prepare.py`、`pro6000_launch.py`、spec、其他源码或 Git 索引。日志和交接材料写在 `.agentmesh`。未运行 GPU 实验，也未声明研究效果通过。保留主代理组织的独立最终 QA 作为本轮审查结束条件。
