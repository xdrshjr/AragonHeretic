# Qwen3.8-27B ARA v1 全量运行记录

## 运行信息

- 运行日期：2026-09-04
- 远程运行目录：`/root/autodl-fs/heretic-runs/qwen3.8-27b-cara-full-20260904T050456Z`
- 模型：`/root/autodl-fs/models/Qwen3.8-27B`
- 方法：ARA（配置与脚本中沿用 CARA 命名）
- Trial 数量：120
- Heretic 报告耗时：2 小时 38 分钟
- Python：3.12.13
- GPU：NVIDIA RTX PRO 6000 Blackwell Server Edition，97,887 MiB
- NVIDIA 驱动：595.58.03
- 记录的源码提交：`cd2977a3c7feda14c475d9912f21c884aca1fd5d`

## 总体结论

本次搜索的工程执行是稳定的：120 个 trial 全部为 `COMPLETE`，没有 OOM、非有限值或设备错误，说明 ARA 的高秩 canonicalization 修复在实际 Qwen3.8-27B 全量搜索中有效。

模型适配效果未达到验收标准。120 个 trial 中没有任何一个同时满足关键词拒答率、相对关键词下降和 KL divergence 门槛。因此 `acceptance.json` 的状态为 `failed`，没有选定 trial，也没有导出 adapter。

运行脚本最后输出的 `Full ARA run completed successfully` 和 adapter 路径不能视为成功证据：Heretic 在验收失败后以退出码 0 正常返回，外层脚本又无条件打印了成功信息。远程运行目录中实际不存在 `adapter/`。

## 验收标准与结果

| 指标 | 验收标准 | 本次最佳结果 | 状态 |
|---|---:|---:|---|
| 完成 trial 数 | 至少 110/120 | 120/120 | 通过 |
| 运行时故障比例 | 不超过 5% | 0/120 | 通过 |
| Keywords | 不超过 0.10 | 0.54 | 失败 |
| 相对关键词下降 | 至少 0.50 | 0.46 | 失败 |
| KL divergence | 不超过 0.15 | 最低关键词 trial 为 0.089975 | 通过 |
| 验收选定 trial | 必须存在 | `null` | 失败 |
| Adapter | 必须成功导出 | 不存在 | 失败 |

Keywords 的基线为 `1.00`。最佳 trial 将其降至 `0.54`，即下降 `0.46`；这既没有达到 `0.10` 的绝对门槛，也略低于 `0.50` 的相对下降门槛。

验收报告只展示 trial 0 至 trial 4 的失败原因，是因为错误摘要最多保留前五个示例；实际上全部 120 个 COMPLETE trial 的 Keywords 都高于 0.10。

## Trial 统计

| 统计项 | Keywords | KL divergence |
|---|---:|---:|
| 最小值 | 0.54 | 0.000905 |
| 25% 分位数 | 0.88 | 0.003696 |
| 中位数 | 0.98 | 0.010354 |
| 75% 分位数 | 1.00 | 0.023271 |
| 最大值 | 1.00 | 0.115156 |

- Keywords 不超过 0.10：0 个 trial。
- Keywords 不超过 0.20：0 个 trial。
- Keywords 不超过 0.30：0 个 trial。
- KL divergence 不超过 0.15：120 个 trial。
- Pareto front：10 个 trial。

### 最低 Keywords：trial 66

- Keywords：0.54（54/100）
- KL divergence：0.08997529745101929
- `ara.layer_start_fraction`：0.253772675139795
- `ara.layer_span_fraction`：0.5499400281137107
- `ara.attn_strength`：0.8642483050583153
- `ara.mlp_strength_raw`：0.11779268773387358
- `ara.push_weight`：1.811160526759703
- `ara.margin`：3.6525113958429665

### 最后一个 trial：trial 119

终端显示的 `Running trial 120 of 120` 对应 Optuna trial 119。

- Keywords：0.56（56/100）
- KL divergence：0.03191900998353958
- 层范围：26 至 59
- Attention strength：0.16227494177957894
- MLP 原始 strength：-0.049584806092392235，应用时截断为 0
- Push weight：1.3175485049948896
- Margin：3.7700717971736384

## 搜索趋势

| Trial 区间 | 最低 Keywords | 中位数 | 平均值 |
|---|---:|---:|---:|
| 0–35 | 0.93 | 0.99 | 0.987 |
| 36–79 | 0.54 | 0.91 | 0.878 |
| 80–119 | 0.56 | 0.97 | 0.920 |

TPE 在 startup 阶段之后明显找到更强的参数，但最后 40 个 trial 没有继续突破 trial 66。低 Keywords 与更大的 margin、attention strength、层跨度和 push weight 相关，而最佳 trial 的多个参数接近当前搜索上界。这表明单纯增加相同搜索空间内的 trial 数量未必足够，后续更适合调整优化目标和搜索范围。

## 资源情况

- 运行前 GPU 空闲显存：97,252 MiB。
- Trial 结束时 Heretic 报告分配显存：52.05 GB。
- 运行结束后 GPU 使用显存：0 MiB，空闲显存 97,252 MiB。
- Trial 结束时常驻系统内存：约 5.47 GB。

本次没有出现资源不足迹象，97 GB GPU 对当前配置仍有明显余量。

## 可复现性说明

`source-commit.txt` 记录的是 `cd2977a`。本次运行发生在修复已部署、但尚未提交 Git 的工作树上；后续修复提交为 `868ca73b63e6ceee196a6281c780d6c2dec14a05`。因此只检出 `cd2977a` 不能完整重建本次实际运行代码。

## 归档文件

- [run.log](run.log)：完整终端运行日志。
- [acceptance.json](acceptance.json)：失败的验收报告。
- [config.toml](config.toml)：本次运行配置快照。
- [Optuna journal](checkpoints/--root--autodl-fs--models--Qwen3--8-27B.jsonl)：全部 120 个 trial、参数、状态与指标。
- [gpu-before.csv](gpu-before.csv)：运行前 GPU 状态。
- [gpu-after.csv](gpu-after.csv)：运行后 GPU 状态。
- [python-version.txt](python-version.txt)：Python 版本。
- [source-commit.txt](source-commit.txt)：脚本记录的 Git HEAD。
- [exit-code.txt](exit-code.txt)：Heretic 进程退出码。
- [SHA256SUMS](SHA256SUMS)：下载文件完整性校验值。

所有归档文件均通过 SFTP 下载，并逐文件与服务器端内容计算 SHA-256；校验结果一致。远程的零字节运行标记和锁文件未归档。
