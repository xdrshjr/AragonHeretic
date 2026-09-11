# Pro 6000 一键实验

在已准备好的 `connect.westd.seetacloud.com:46241` 服务器登录后，执行：

```bash
bash /root/autodl-fs/AragonHeretic-v3/scripts/run_qwen38_27b_ara_v3_pro6000.sh
```

命令立即返回后台 PID 和运行目录。后台进程独立于 SSH 会话，日志实时写入
运行目录下的 `run.log`。首次启动会核对 GPU、缓存数据、模型文件摘要和依赖版本，
然后生成全量角色划分、冻结本次源码与配置，再加载模型开展搜索。

## 默认配置

| 项目 | 配置 |
| --- | --- |
| 方法 | Heretic-ARA v3，S2 / sequential-refresh |
| 模型 | `/root/autodl-fs/models/Qwen3.8-27B`，固定 revision |
| 设备 | 单张 RTX PRO 6000 96 GiB |
| 数值 | NF4、BF16 计算、FP32 LoRA、rank 128 |
| 搜索 | 24 次：4 个锚点、8 次随机、12 次约束 TPE |
| 刷新 | 最多 2 轮，每轮刷新轨迹，保持基座参考 |
| 种子 | 42 |
| fit | 每侧候选池 192 条，按种子选 96 条 |
| monitor | 每侧 64 条 |
| mechanism-development | 每侧 44 条，保留供机制分析 |
| development | 每侧 100 条 |
| 预算 | 默认累计 48 小时，单 GPU 计费 |
| 输出根目录 | `/root/autodl-fs/heretic-runs` |

“全量”指以上 v3 实验规格，不是把数据源所有训练行都用于拟合。
划分保留已完成 pilot 的各角色题目，再按原始行号补齐 fit 和 monitor；
每侧共有 400 条互斥题目，按规范化正文排除重复。
development 题目和系统提示保持不变，不按候选结果重新选题。

48 小时是硬预算上限，**不是运行耗时预测**。全量耗时尚未实测。
预算包括模型加载、基线计算、搜索和导出；准备及文件校验不占 GPU 运行预算。
需要更长预算时在创建新实验时指定，例如：

```bash
bash /root/autodl-fs/AragonHeretic-v3/scripts/run_qwen38_27b_ara_v3_pro6000.sh --hours 96 --seed 42
```

种子可选 42、43、44，每次创建新目录。预算、种子、模型和源码提交随后冻结；
恢复时不能追加参数改变原实验。配置支持 `--model`、`--run-root`、
`--pilot-record`，环境路径可通过 `HERETIC_RESEARCH_PYTHON` 覆盖。
默认路径已经匹配当前服务器，不需要手动填写。

## 查看、准备与恢复

下文的 `运行目录` 替换为启动时打印的绝对路径。

```bash
# 查看阶段、进程存活和已完成/失败次数
bash /root/autodl-fs/AragonHeretic-v3/scripts/run_qwen38_27b_ara_v3_pro6000.sh --status 运行目录

# 查看实时日志；退出 tail 不会停止实验
tail -f 运行目录/run.log

# 只准备全量输入并执行真实预检，不加载模型或启动搜索
bash /root/autodl-fs/AragonHeretic-v3/scripts/run_qwen38_27b_ara_v3_pro6000.sh --prepare-only

# 从已准备或中断的目录继续，使用目录中的冻结源码与剩余预算
bash /root/autodl-fs/AragonHeretic-v3/scripts/run_qwen38_27b_ara_v3_pro6000.sh --resume 运行目录
```

阶段通常为 `prepare → preflight → baseline → search → export → completed`。
基线阶段需要加载 27B 模型并生成参考响应，可能持续较长时间。
`ready` 表示只准备模式已通过。`failed` / `interrupted` 需结合日志排查。
启动命令返回成功代表后台进程已提交，最终退出码保存在 `exit.json`。
硬预算看门狗强制退出时另存 `budget-timeout.json`，状态命令会明确显示预算耗尽。

同一目录或同一 GPU 的重复运行会被锁拒绝。恢复保留已完成 trial；
中断时正在执行的 trial 按原生 v3 规则记为一次失败并占用 24 次预算，
从下一个 trial 继续。不会无限重试失败参数。正常 TERM 中断按实际时间结算；
掉电或 SIGKILL 时，未知停机时间会保守计入累计预算，最多记到原截止时间。

每次实验从 Git HEAD 归档源码至 `source/`，主工作区后续更新不影响运行。
未提交修改不会进入实验；不要改动运行目录下的冻结源码、协议或配置。
完成目录再次恢复时会验证已有导出文件，不重复搜索。

## 结果文件

| 文件 | 用途 |
| --- | --- |
| `run.json` | 源码提交、模型路径、种子、预算及运行身份 |
| `prepared.json` | 全量样本数、硬件、磁盘和预检对应的协议摘要 |
| `protocol/protocol.json` | 冻结角色、数据与源码摘要、实验范围 |
| `source/config.pro6000-experiment.toml` | 本轮实际运行配置 |
| `search/study.json` | 24 次参数、终态、开发指标和失败原因 |
| `search/search/<attempt>/` | 每次尝试的层组事件及因子快照 |
| `budget.json` | 跨启动累计单 GPU 预算 |
| `experiment-result.json` | 完成次数、所选候选、指标、导出路径及摘要 |
| `artifacts/best-adapter/` | PEFT LoRA 适配器与 tokenizer 文件 |

搜索沿用原生 v3 求解器、数值约束和随机/TPE 策略；优先从通过开发数值门槛
的候选中按 Keywords、首 token KL、尝试序号选择。没有候选达到门槛时，
仍导出开发指标最佳的对照适配器，并明确标记 `numeric_gate_passed=false`。
该导出只表示开发集比较结果，不能据此推断已经改善拒答或保持通用能力。

运行完整登记 24 次且至少 23 次成功，才标记工程状态 `completed`；
否则保留结果并返回失败。所有结果的 `research_status` 均为 `not_run`，
不会生成论文验收通过或研究合格候选。独立审计、语义双评以及原研究入口的
8 小时预算证明仍保留在研究流程内；本实验协议不能直接传给研究搜索入口。

磁盘预留依据全部目标 LoRA shape，包含剩余 trial、一次适配器导出和 20% 余量。
新实验约需 30.94 GiB 可用空间；已有快照不会在恢复时重复预留。
文件保存在共享盘，便于实例关闭后继续保留。
