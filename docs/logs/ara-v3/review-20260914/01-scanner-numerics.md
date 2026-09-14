# 数值、事务与评分扫描结论

日期：2026-09-14。职责：Scanner；仅扫描指定七个模块的本轮增量及相关调用和测试，按 `docs/plans/ara-v2-refusal-optimization/spec.md` v3.2 核对。未修改源码、未访问远端、未运行任务上报工具。以下为待 Reviewer/QA 独立复核的发现。

发现：P0 0 项，P1 1 项，P2 1 项。

## NUM-001：阶段测量重置 CUDA 峰值，使冻结显存上限漏检

- 类别：BUG / 资源约束；建议严重度：P1。
- 位置：`src/heretic/ara_refinement_capture.py:474-476`、`:489-493`、`:516-520`；调用路径 `src/heretic/ara_refinement.py:314-325`。
- 触发：`_block_transaction` 顺序执行 `monitor_before`、`capture`、`local_optimization`，每个顶层 `measure_research_phase` 都调用 `torch.cuda.reset_peak_memory_stats`。这几个阶段之间没有 `check_budget`。若 capture 峰值超过冻结 `max_cuda_allocated_gib`，释放临时张量后进入 optimization，前一峰值被清除。后续 `research_resource_status` 仅读取重置后的峰值，允许已经超限的 trial 继续。
- 证据：测量 finally 将真实峰值写入记录，但不验证上限；资源 guard 仅读取 CUDA 的当前峰值计数器，没有读取历史阶段记录或独立高水位。
- 实际隔离复现：从当前文件 AST 抽取原样的 `measure_research_phase` 和 `research_resource_status`，注入 CUDA 计数器及 psutil 桩。冻结上限为 22 GiB，capture 峰值为 23 GiB，下一 optimization 峰值为 6 GiB。输出为：

```text
{'recorded_capture_peak_GiB': 23.0, 'frozen_limit_GiB': 22,
 'guard_result': 'passed', 'reported_peak_GiB': 6.0}
```

- 影响：新测量逻辑削弱原显存限制，保存的阶段证据已经证明超限，却仍可能进入 COMPLETE 和后续候选流程。规格要求资源异常中止 attempt，而非仅事后留诊断记录。
- 建议：每个阶段结束、重置下一阶段计数器之前验证已采集峰值，或将其更新到贯穿 trial 的独立每设备高水位并令 guard 读取该值；保持嵌套阶段不重复 reset。测试应覆盖“前一阶段超限、下一阶段降低”的真实序列，不能只测试当前峰值超限。

## NUM-002：回溯异常丢失已经取得的逐次失败证据

- 类别：ERROR_HANDLING / 诊断可观测性；建议严重度：P2。
- 位置：`src/heretic/ara_backtracking.py:258-270`、`:287-288`；`src/heretic/ara_refinement.py:428-436`、`:471-497`；外层 `src/heretic/ara_research_runner.py:315-319`。
- 触发：候选已临时安装、真实联合输出和 prediction_discrepancy 已算出，随后 monitor 抛错，或 `_evaluate_attempt` 中第二次预算检查抛错。`_attempt` 的 finally 恢复 A/B，但异常继续传播，绕过 reasons/return；`evaluate_block_proposals` 仅在正常返回后才将 row 加入 event。再外层仅正常返回的 block 才加入 events，只有 trial COMPLETE 才写 trial.json。
- 证据：runner 异常分支仅保存异常类型和字符串，未接收或持久化局部 event。因此本次 alpha、已评估 guards、真实误差、阶段测量、恢复 hash，以及更早已完成组的事件，均无法从 FAIL 行恢复。
- 影响：不构成错误通过，完整因子恢复路径仍存在；但违反 §3.3 与 BlockEvent 对预算/数值/运行失败归因、已经计算的误差和成本证据的要求，尤其影响昂贵失败运行的诊断与资源证明。
- 建议：进入尝试时即登记可变 row；在 finally/异常边界补全失败原因和恢复身份，并由 trial 边界原子保存 FAIL 证据后继续抛出原异常。不能将异常包装成普通回溯拒绝。新增测试应断言 monitor/预算异常后既恢复模型，又保留失败尝试及已有事件。

## 逐文件状态

| 文件 | 本轮结论 |
| --- | --- |
| `src/heretic/ara_proposal.py` | 未发现有证据的 P0/P1/P2：小矩阵 QR/SVD、谱裁剪与统一缩放、有效权重拼接插值、rank 截断、零模块初始化和重放有效权重比较的主公式符合规格。 |
| `src/heretic/ara_backtracking.py` | NUM-002；正常拒绝和评分异常的 A/B 恢复路径存在。 |
| `src/heretic/ara_refinement.py` | NUM-001 的阶段调用路径、NUM-002 的事件持久化路径；基座保留项、绝对替换、原 monitor 阈值和旧 reject 分派未发现其他证据充分的问题。 |
| `src/heretic/ara_refinement_capture.py` | NUM-001；既有禁用上下文、配对捕获恢复和本轮剩余快照验证未发现其他新增问题。 |
| `src/heretic/continuation_scores.py` | 未发现有证据的新增问题：选择原因果位置后分块，完整词表归一化，保持原长度均值与顺序。 |
| `src/heretic/sequence_scores.py` | 未发现有证据的新增问题：复制 CPU continuation 切片避免保留完整 prompt 存储；KL 方向、有效位置和先 token 后 prompt 分母保留。 |
| `src/heretic/ara_runtime.py` | 本轮增量增加 CUDA 同步，未发现独立新增问题；与 NUM-001 共享的底层峰值计数器需要一并保持语义一致。 |

测试审阅覆盖 `test_ara_proposal.py`、`test_ara_backtracking.py`、`test_ara_refinement.py`、`test_ara_refinement_capture.py`、`test_refusal_log_odds.py`、`test_sequence_scores.py`。现有回溯测试确认恢复，却不检查异常事件保留；缺少跨阶段显存峰值重置的资源回归。本地默认 Python 为 3.14.6 且没有 torch，本扫描仅执行上述不依赖 torch 的真实函数桩复现；主代理另行执行的 CPU 测试不重复记为本 Scanner 的运行证据。未运行 GPU，未评价 27B 模型效果或真实资源容量。
