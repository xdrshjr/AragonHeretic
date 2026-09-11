# ARA v3 单卡小样本启动验证

状态：小样本启动验证通过；完整 pilot 继续在后台运行。正式研究状态仍为 `not_run`。

2026-09-11 03:31 UTC 检查时，原生脚本已完成前两个层组（32 个真实模块）的求解和有效更新检查，候选因部署门槛被正常拒绝、回滚，并继续处理第三组。此前的 canonicalization 异常未复现。证据见 [startup-summary.json](startup-summary.json)。
这里的 `proposal_rejected` 是候选未被接受，不是进程故障；本记录只确认方法能够正常启动和执行，不宣称获得有效改进。
已观测 CPU RSS 峰值 52.36 GiB（加载阶段），GPU 显存峰值 45,770 MiB。检查时 worker PID 为 `4157`，受跟踪的源码工作区无修改。

## 环境与入口

- 服务器：本次用户提供的 AutoDL 实例，主机名 `autodl-container-94dd45abcd-88709ae8`。
- GPU：单张 NVIDIA GeForce RTX 4090，驱动报告显存 49,140 MiB。
- 项目：`/root/autodl-fs/AragonHeretic-v3`；既有旧项目的本地修改已保留。
- 已同步代码：`685e26cabc305ffc7449a79e44652467b823d12c`。
- Python：`/root/autodl-fs/conda-envs/heretic-dsv4/bin/python`。
- 模型：本地缓存的 Qwen3.8-27B，revision `1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0`，`bnb_4bit`、BF16、LoRA FP32/rank 128。
- 原生脚本：`scripts/run_ara_research_96.sh`，阶段 `--phase pilot`，方法 `S2 / sequential-v3 / sequential-refresh`。
- 运行配置：[config.toml](config.toml)，实际位于项目根目录 `config.qwen38-27b-cara-v3-single-gpu-pilot.toml`。

fit 和 monitor 各侧各 8 条真实数据；机制开发各侧 44 条、development 各侧 100 条仍沿用协议规定数量。所有角色互斥，模型文件和源码逐项做摘要绑定。[protocol.json](protocol.json) 记录身份、摘要和环境。
本轮只执行首个锚点 pilot，不执行配置中正式搜索的 24 次 trial；不读取审计角色，不执行语义双评或能力审计。

## 已发现并修复的问题

1. 模板使用 glob 风格目标名，而运行时按字面子串匹配，导致 128 个目标模块无法通过检查。提交 `d07b34a` 修正三种投影名称，16 项配置测试通过。
2. 单卡加载过程中 CPU RSS 峰值约 52.36 GiB，超过原捕获限额 32 GiB。在本机 1 TiB 内存条件下，将运行配置的捕获限额设为 64 GiB，与 v3 总进程上限一致。
3. FP32 QR/SVD 在 rank 128 时产生过大的有效更新误差。提交 `685e26c` 让 v3 复用已有 FP64 分解，部署因子仍为 FP32，一致性检查仍为 `rtol=1e-6, atol=1e-7`。另外输出层组开始/结束进度，方便观察长任务。

新增回归在修复前于 CPU/CUDA 均复现相同异常；修复后 45 项相关测试通过。实际 GPU 模块宽度 6144/17408 的合成探针也验证了精度改善。证据见 `canonical-probe.log`、`numerical-regression-before.log` 和 `numerical-regression-after.log`。
原始失败、retry1、retry2 的结果与配置单独保留，失败没有改写成成功。

## 服务器上的查看方法

```bash
record=/root/autodl-fs/heretic-runs/ara-v3-smoke-20260911
tail -f "$record/pilot-retry3.log"
cat /root/autodl-tmp/ara-v3-smoke-20260911/pilot-retry3/pilot-result.json
cat "$record/bootstrap-retry3.exit"
```

运行中的结果文件、退出码文件可能尚不存在。完成后后台汇总器会核验真实退出码、trial 状态及快照 SHA-256，输出 `pilot-summary.json`、`layer-group-events.json`；其日志为 `finalize-retry3.log`。

本轮实际调用的原生入口如下（已有任务运行时不要重复启动）：

```bash
cd /root/autodl-fs/AragonHeretic-v3
export HERETIC_RESEARCH_PYTHON=/root/autodl-fs/conda-envs/heretic-dsv4/bin/python
export HF_HOME=/root/autodl-fs/hf-cache
export HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1
export OMP_NUM_THREADS=8 MKL_NUM_THREADS=8 OPENBLAS_NUM_THREADS=8
bash scripts/run_ara_research_96.sh \
  --config config.qwen38-27b-cara-v3-single-gpu-pilot.toml \
  --run-dir /root/autodl-tmp/ara-v3-smoke-20260911/pilot-retry3 \
  --phase pilot
```

新的独立运行必须使用新协议和新目录。服务器上的 `retry_pilot.sh` 配合 `prepare_retry.py` 会生成新的协议摘要；旧协议累计预算和旧结果保留。
