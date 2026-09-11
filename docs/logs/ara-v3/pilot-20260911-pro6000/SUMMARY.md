# ARA v3 迁移至 RTX PRO 6000

状态：run2 完整工程 pilot 通过，原生退出码和后台启动器退出码均为 0，trial 状态为 `COMPLETE`。5 个层组求解、development 评估和最终快照保存全部完成，GPU 已释放。结果见 [pilot-summary.json](pilot-summary.json)。
原生退出记录见 [exit.json](exit.json)，原生预算账本见 [pilot-budget.json](pilot-budget.json)。

本轮未接受任何层组更新，拒答关键词率仍为 100%，不能据此声称方法效果达标。run1 的数值异常、后续精度修复和失败证据均保留。

run1 的协议为 `b375d884f243543186d67928fe6a27398a4ce179a678aecb30ae436bec1bf403`，详见 [run1-protocol.json](run1-protocol.json)、[run1-config.toml](run1-config.toml)、[run1-preflight-result.json](run1-preflight-result.json) 和 [run1-failure.json](run1-failure.json)。

数值诊断见 [canonical-diagnosis.json](canonical-diagnosis.json)：相同真实 FP32 因子，FP32 检查有 1 个元素超过原阈值，而 FP64 检查的越界元素为 0，最大误差/容差为 0.05710。
提交 `a6af663762f2fa6a543110f78ec3c74ea4bce227` 仅提高分块检查的计算精度，LoRA 部署仍为 FP32，`rtol=1e-6`、`atol=1e-7` 及算法门槛保持不变。验证见 [canonical-fix-tests.log](canonical-fix-tests.log) 和 [canonical-fix-verification.json](canonical-fix-verification.json)。

run2 的协议为 `0f8a57e633de47dd4b406cd4ecc38f8d4f95d55fb0d6dd92373a61f61f8d32fd`，详见 [protocol.json](protocol.json) 和 [config.toml](config.toml)。实际项目上的 24 项启动检查通过，原生预检通过，见 [environment-tests.log](environment-tests.log) 和 [preflight-result.json](preflight-result.json)。

## 完整运行结果

| 项目 | 实测结果 |
| --- | --- |
| 结束时间 | 2026-09-11 07:15:31 UTC |
| 原生预算墙钟 | 2597.67 秒，约 43 分 18 秒，包含模型加载和基线；不含前置协议校验 |
| trial 墙钟 | 1328.41 秒，约 22 分 8 秒，包含求解与 development 评估 |
| 层组 / 模块 | 5 组 / 72 个模块全部完成 |
| sweep / 接受层组 | 执行 1 个 sweep；接受 0 组，按规则提前停止后续 sweep |
| development 分母 | bad 100 条、good 100 条；结果记录的 `sample_count=100` 对应 bad |
| 拒答关键词率 | 基线 1.0，最终 1.0 |
| 拒答 log-odds | 4.967287063598633 |
| 首 token KL / 序列 KL | 均为 0.0 |
| 采样显存峰值 | 91,903 MiB，约 89.7 GiB |
| 进程 CPU 高水位 | 52.40 GiB |
| 最终快照 | 1,107,384,703 字节，约 1.03 GiB；原始文件及归档 SHA-256 一致 |

显存峰值来自每 15 秒采样的 [telemetry-run2.jsonl](telemetry-run2.jsonl)，包含进程实际占用，不能与仅限制 CUDA allocated 的 88 GiB 门槛混为同一指标。原生预算器默认按两卡保守计费为 1.44315 GPU 小时，本次实际单卡墙钟折算为 0.72157 GPU 小时；两者都不是实测利用率积分。

[layer-group-events.json](layer-group-events.json) 保留不含响应正文的数值事件：72 个模块的累计偏移比例均未超过 0.60，最大为 0.244572；但 72 个模块的最大奇异值都超过上限 8.0，整体最大为 27.274326，导致 5 组均以 `proposal_rejected` 正常回滚。此次未改动这些接受门槛，也未开展后续调参、正式搜索、独立审计或语义双评。

最终快照 SHA-256：`9145166d55f2593b54ae3d1c1d4f64f07c0eda51fb2d63d6004a59d2f77a01ac`。源码/配置文件摘要、协议身份、评分分母和有限值、退出码、快照文件及归档摘要均已核验。

## 迁移背景

RTX 4090 上的前一轮已完成 5 个层组求解，但在 development 评估时发生 CUDA OOM，退出码 3，未产生成功的 trial 或最终快照。用户要求改用 Pro 6000。

## 本轮环境与参数

- 主机：`autodl-container-f7964f8eb7-61323a17`。
- GPU：NVIDIA RTX PRO 6000 Blackwell Server Edition，97,887 MiB 显存；驱动 `580.82.09`。
- 内存：1 TiB；复用共享数据盘上的项目、Conda 环境、模型和数据缓存。
- run1 源码：`83b684146cbd4dfd7e037e42bc8c0129dac759ac`；run2 源码：`a6af663762f2fa6a543110f78ec3c74ea4bce227`。
- 项目目录：`/root/autodl-fs/AragonHeretic-v3`。
- Python：`/root/autodl-fs/conda-envs/heretic-dsv4/bin/python`。
- 模型：Qwen3.8-27B，`bnb_4bit`、BF16，LoRA FP32 / rank 128。
- 方法：`sequential-v3`，S2 / sequential-refresh，最多 2 个 sweep。
- 数据：沿用前一轮同一批角色源文件，fit 和 monitor 各侧 8 条，mechanism-development 各侧 44 条，development 各侧 100 条。
- 模型加载预算与 CUDA allocated 门槛：44 GiB → 88 GiB；CPU RSS 上限仍为 64 GiB。

模型、数据、量化类型和锚点参数保持相同；新机器使用独立协议、预算和输出目录。协议摘要会参与因子随机种子的派生，因此不声称两轮初始因子逐位相同。
原始角色输入、模型文件和源码摘要仍由原生准备/运行入口检查。只执行 pilot，不执行正式搜索、独立审计或语义双评。

## 入口和运行记录

原生入口仍为 `scripts/run_ara_research_96.sh --phase pilot`。
`pilot` 阶段只运行固定锚点 attempt 0；配置中的 `n_trials=24` 不会使这次执行变成 24 次正式搜索。

```bash
cd /root/autodl-fs/AragonHeretic-v3
export HERETIC_RESEARCH_PYTHON=/root/autodl-fs/conda-envs/heretic-dsv4/bin/python
export HF_HOME=/root/autodl-fs/hf-cache
export HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1
export OMP_NUM_THREADS=8 MKL_NUM_THREADS=8 OPENBLAS_NUM_THREADS=8
bash scripts/run_ara_research_96.sh \
  --config config.qwen38-27b-cara-v3-pro6000-pilot-run2.toml \
  --run-dir /root/autodl-tmp/ara-v3-pro6000-smoke-20260911/pilot-run2 \
  --phase pilot
```

本轮已由独立后台启动器执行上述流程；该命令作为记录，不能在原目录重复启动。

```bash
record=/root/autodl-fs/heretic-runs/ara-v3-pro6000-smoke-20260911
tail -f "$record/pilot-run2.log"
cat "$record/bootstrap-run2.exit"
cat "$record/pilot-summary.json"
```

后台核验器已完成检查，并生成 `pilot-summary.json` 与不含响应正文的 `layer-group-events.json`。快照及原生结果已归档至共享持久数据盘：

`/root/autodl-fs/heretic-runs/ara-v3-pro6000-smoke-20260911/artifacts/pilot-run2/`

其中 `factors.pt` 已完成复制及 SHA-256 验证；完整原生结果仅保留在服务器，仓库记录不包含模型回答正文。
正式方法效果和研究验收不包含在工程 pilot 通过结论中。
