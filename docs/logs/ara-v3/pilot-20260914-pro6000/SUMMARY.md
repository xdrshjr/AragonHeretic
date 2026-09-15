# ARA v3 Qwen3.8-27B Pro6000 小样本 Pilot（2026-09-14）

## 结论

本轮最新代码小样本工程 pilot 已完整结束，原生退出码和自动收尾退出码均为 `0`，trial 状态为 `COMPLETE`，没有失败分类。实验流程、协议检查、模型加载、五个层组求解、development 评估、快照保存和持久盘归档均已跑通。

但本轮没有接受任何层组更新：五个提案均因 `proposal_rejected` 回滚，最终 adapter 身份与初始身份相同。因此这是一次“工程流程通过、适配效果未产生”的 pilot，不能据此声称 ARA v3 已改善模型行为。

## 实验身份

| 项目 | 值 |
| --- | --- |
| 远端代码提交 | `0ab032c8acdee8a0f197d4ffba8bec1bad93caad` |
| 模型 | Qwen3.8-27B |
| 方法 | ARA `sequential-v3`，S2 / `sequential-refresh` |
| 量化与精度 | `bnb_4bit`，BF16 模型，LoRA rank 128 |
| 协议哈希 | `469022a4a90f8ae94b54df7c8a7443f048e3c6aaaedef1a65b5a63214f16b747` |
| 参数身份 | `79312ce48da6495086e38191c792a2bb7767449295cd9242e2d1a1944d285a98` |
| 主机 | `autodl-container-f7964f8eb7-61323a17` |
| GPU | NVIDIA RTX PRO 6000 Blackwell Server Edition，97,887 MiB |

数据角色数量为：fit 每侧 8 条、monitor 每侧 8 条、mechanism-development 每侧 44 条、development 每侧 100 条。本轮 pilot 只执行固定锚点 attempt 0；配置中的 `n_trials=24` 不代表执行了 24 次正式搜索。

## 时间与资源

| 项目 | 结果 |
| --- | --- |
| 后台 pilot 启动 | 约 2026-09-14 06:07:42 UTC |
| 原生运行结束 | 2026-09-14 06:52:29 UTC |
| 自动归档结束 | 2026-09-14 06:52:49 UTC |
| 端到端 pilot 时间 | 约 44 分 47 秒 |
| trial 核心墙钟 | 1,345.47 秒，约 22 分 25 秒 |
| 最终 CPU RSS | 8.53 GiB |
| 最终 CUDA allocated / reserved | 51.31 / 89.05 GiB |
| 归档结果大小 | 1,108,127,607 字节，约 1.03 GiB |

末期 evaluation 出现一次 CUDA 分配告警：申请 8,130,658,304 字节时设备空闲约 5,607,653,376 字节。运行器随后正常完成并写出退出码 `0` 和完整评分，因此该告警不是本轮更新为零的直接原因，但说明当前评估流程的显存余量偏紧。

## 层组结果

实验覆盖第 16–51 层，共 72 个目标模块，分成五组执行。所有模块的累计更新比例均低于 `0.60` 上限；但每组内所有模块的最大奇异值都超过配置上限 `8.0`，所以五组均按事务规则回滚。

| 层组 | 层范围 | 模块数 | 最大奇异值范围 | 最大更新比例 | 结果 |
| --- | --- | ---: | ---: | ---: | --- |
| 0 | 16–23 | 16 | 13.5758–27.6307 | 0.246108 | 拒绝并回滚 |
| 1 | 24–31 | 16 | 13.1904–24.2979 | 0.222125 | 拒绝并回滚 |
| 2 | 32–39 | 16 | 17.4264–25.0717 | 0.241391 | 拒绝并回滚 |
| 3 | 40–47 | 16 | 14.4479–25.8211 | 0.235878 | 拒绝并回滚 |
| 4 | 48–51 | 8 | 17.5906–24.4319 | 0.244291 | 拒绝并回滚 |

最终 adapter 身份为 `fbd3b42d2e6d39bcd171a34ba37f00a1858dbd166c575263119f97f721d9c71c`，且每组更新前后的身份均一致，证明回滚生效。

## 最终评分

| 指标 | 结果 |
| --- | ---: |
| baseline keywords | 1.0 |
| final keywords | 1.0 |
| refusal log-odds | 4.967287063598633 |
| first-token KL | 0.0 |
| sequence KL | 0.0 |
| development 评分分母 | 100 |

这些分数与 adapter 身份共同表明最终评估的是未发生有效更新的基线状态，而不是成功适配后的模型。

## 远端证据与本地归档状态

远端持久盘记录目录：

```text
/root/autodl-fs/heretic-runs/ara-v3-pro6000-latest-pilot-20260914T055809Z
```

远端持久盘完整结果：

```text
/root/autodl-fs/heretic-runs/ara-v3-pro6000-latest-pilot-20260914T055809Z/result
```

远端 `factors.pt` 大小为 1,107,384,703 字节，SHA-256 为 `b788e6d0dbf86e9059657baceda23a528e4e1ba67cda4ff9db3c1576b5a062b6`。实例恢复后已对持久盘归档文件重新执行 SHA-256 计算，结果与结构化记录完全一致。该大文件未纳入 Git 文档目录。

本地总结依据实验结束后通过固定 SSH 主机指纹读取的原生结果生成。实例恢复后，以下原始证据已通过 SFTP 补齐：

- `pilot.log`：完整运行日志；
- `pilot-result.json`：原生结构化 pilot 结果；
- `protocol.json` 与 `preparation.json`：冻结协议和准备输入；
- `config.toml`：实际运行配置；
- `preflight.log` 与 `preflight-wrapper-attempt1.log`：预检记录及首次包装脚本导入路径错误；
- `exit.json`、`pilot-budget.json` 与 `experiment.json`：退出码、预算账本和实验路径；
- `run-pilot.sh` 与 `run-preflight.sh`：实际使用的启动脚本。

`pilot-result.json` 和 `pilot.log` 含模型响应正文；审阅或发布时应优先使用本文件、`run-summary.json` 和 `layer-group-summary.json` 中的精简结果。

## 后续建议

下一轮应优先降低提案强度，或在事务提交前加入奇异值投影/裁剪，使更新满足既有 `max_singular_value=8.0` 安全门。没有独立证据前，不建议直接放宽安全阈值。同时应降低 evaluation 峰值显存或在阶段切换时主动回收缓存，以消除末期分配告警。
