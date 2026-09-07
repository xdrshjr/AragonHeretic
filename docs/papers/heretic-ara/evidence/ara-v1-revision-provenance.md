# ARA v1 论文修订溯源

核验日期：2026-09-07。本记录对应本次修订，历史 2026-09-01 的评审、
分析与取证文件保留原貌。研究对象是源码形式化、本地适配和一次失败验收实验。
本次仅离线重算归档标量，未运行模型、访问 SSH 或产生新模型响应。

## 三个版本的身份

| 名称 | 固定身份 | 使用边界 |
|---|---|---|
| 上游 ARA 原型 | `edc3b123456c7f86f24d409b838ab3a7226e285e` | 历史硬 kNN、全矩阵/LoRA、完整行归一化、仅 B 重置及分段外层分数的分析；没有本次模型测量 |
| 本地 point-v1 | 2026-09-04 归档记录 HEAD `cd2977a3c7feda14c475d9912f21c884aca1fd5d`；后续修复提交 `868ca73b63e6ceee196a6281c780d6c2dec14a05` | 唯一实测版本；运行时含已部署但尚未提交的修复，不能由记录 HEAD 完整恢复 |
| 本地 trajectory-v2 | `2871bc19050617377f011ecbf1d64c440f2d6a96` | 当前源码与 `config.qwen38-27b-cara-v2.toml` 的后续协议；指定归档不含其效果结果 |

已执行 `git rev-parse HEAD`，输出为上述 v2 完整提交；执行
`git diff 868ca73b63e6ceee196a6281c780d6c2dec14a05 2871bc19050617377f011ecbf1d64c440f2d6a96 -- src/heretic/ara.py`
得到空差异。因而该模块可用于核对 point-v1 数学式；这不证明后续修复提交
是整个运行工作树的精确快照。历史编排、参数解析和评分以 `git show
868ca73:<path>` 单独核对，不用当前拆分后的模块反推历史行为。

## 公式与函数映射

以下 `ara.py` 行号按上述当前固定提交计数，也适用于已核对的修复提交。

| 论文内容 | 源码位置 | 核验结果 |
|---|---|---|
| 确定性 A/B 初始快照与每 trial 恢复 | `src/heretic/ara.py:308–352`，`snapshot_adapter_state` / `restore_adapter_state` | A 按模块名派生种子初始化、B 置零，完整恢复两因子并清梯度；有别于上游仅 B 重置 |
| 模块尺度 | `src/heretic/ara.py:482–513`，`build_calibration_bank` | 良性输出逐列去均值后，对全部元素平方取均值，再下限截断至 `1e-6` |
| 缓存输出加适配增量 | `src/heretic/ara.py:544–594`，`_updated_outputs` / `calculate_ara_loss` | `Y + (X @ A.T) @ B.T` 保留缓存基座响应及其中固定偏置；仍假设缓存输入不变 |
| 软邻域 | `src/heretic/ara.py:516–541`，`soft_neighbor_distance` | 平方欧氏距离除以输出维度及模块尺度；负舍入误差截断；`logsumexp` 保留 `log(n)` 归一化 |
| 四项损失 | `src/heretic/ara.py:554–594`，`calculate_ara_loss` | keep、pull、正 softplus 间隔项、`1e-4` Gram 平衡项；所有平方均先逐元素平方再取均值 |
| 固定 Gram 分母 | `src/heretic/ara.py:741–785`，`optimize_ara_module` | 模块入口以 detach 后初始 Gram 值及 `1e-6` 下限固定；闭包不随当前因子重算分母 |
| 优化与下降检查 | `src/heretic/ara.py:692–785`，`_run_optimizer` / `optimize_ara_module` | 一次 `step(closure)`，`max_iter=20`、history 10、strong-Wolfe；优化后和规范化后允许 `initial + 1e-6` 的绝对容差 |
| 高秩规范化 | `src/heretic/ara.py:621–666`，`_canonical_factors` / `_canonicalize` | float64 reduced QR 与小 SVD，回写 float32；校准输入上相对范数误差不超过 `1e-5`、相对最大误差不超过 `1e-4`，分母下限 `1e-6` |
| 事务回滚及跳过 | `src/heretic/ara.py:741–785` 及历史 `trial_methods.py` 调用路径 | 失败恢复因子；只优化选中半开层区间中强度为正的组件，MLP 原始负强度截断为零并跳过 |

因 `log(mean(exp(-d/tau))) <= 0` 且各项权重非负，point-v1 目标在精确
算术下下界为零；没有上界保证，因子化仍非凸。源码 docstring 中的
“bounded”只能按这个下界含义解释。软邻域不继承旧硬 top-k 邻居切换反例；
截断与数值保护也不支持全局光滑或收敛定理。校准输入上的输出误差检查
是数值一致性保护，不能证明未见输入上等价或修复造成全部运行稳定性。

## 历史评分与搜索

历史 `868ca73:src/heretic/scorers/keyword_rate.py:120` 的 `_is_match`
把空白输出记为匹配，否则转小写、去星号、统一弯引号及空白，再检查配置
标记的子串出现。Keywords 是匹配数除以提示数，存在词法误报和漏报，
不能改称人工语义拒答率或语义攻击成功率。

`868ca73:src/heretic/scorers/kl_divergence.py:58` 使用
`F.kl_div(edited_logprobs, baseline_logprobs, reduction="batchmean", log_target=True)`；
方向是基座分布相对编辑分布的首 token KL。两个原始标量直接最小化，
不使用上游原型的分段归一化外层目标。

`868ca73:src/heretic/main.py:612–617` 构造 TPE：startup 36、候选 128、
multivariate true、seed 42，约束回调为历史 `trial_methods.failure_constraint`
（定义从第 509 行开始）。归档全部 `constraints=[0.0]` 表示没有记录到数值失败，
并不表示效果验收通过。协议仅有 ID 0–35 startup 和 36–119 adaptive TPE
两阶段；任何进一步窗口划分都是事后描述，不能解释为算法阶段效果。

## 归档完整性与证据优先级

原始清单：`../../../logs/ara-v1/SHA256SUMS`，原始字节 SHA-256：
`1ff138f8be7c4361a828fadc93db071c8357d10e793d8520be669862d58f1a29`。
该 pin 用于检测变更，不是外部真实性签名。已逐字节校验全部九项：

- `acceptance.json`
- `checkpoints/--root--autodl-fs--models--Qwen3--8-27B.jsonl`
- `config.toml`
- `exit-code.txt`
- `gpu-after.csv`
- `gpu-before.csv`
- `python-version.txt`
- `run.log`
- `source-commit.txt`

journal 共 2166 条事件，操作计数为 0:1、2:5、4:120、5:720、6:120、
8:1080、9:120。分数与参数取自 journal，设置取有效 settings 并交叉核对
TOML、study manifest 和日志，验收状态取 `acceptance.json`。生成的
`ara-v1-summary.json` 保存每项期望/实算哈希，`ara-v1-trials.csv` 保留
所有候选。完整复核与异常用例在 `test_summarize_ara_v1.py`；最终执行结果
记入 `../paper-output/review-logs/ara-v1-revision-review.md`。

归档二级摘要 `SUMMARY.md` 对规范化修复“有效”、搜索相关性与显存余量有
比原始证据更强的解释。本修订不据此作因果论断：仅报告本次 120/120
COMPLETE、零非 COMPLETE、零联合通过；机制解释列为待检验假说。

关键词分母 100 由每 trial 的 `rich_display`、`md_display` 和数值比率互证。
KL 的 100 个提示依据是配置 split 与加载日志，归档没有逐 trial 样本数记录、
响应或 logits；这次重算不是评分过程的独立再现。

`acceptance.json` 为 `cara-acceptance-v1`，status failed，selected trial、
parameters、validation/audit/reload/replay scores、parameter comparison、
score drift、module counts、resource peaks、environment versions、failure trials
及 artifact hashes 均为 null。它与零联合过门槛相符。退出码 0 和 wrapper
成功字样仅说明进程正常返回；adapter 未导出据归档运行报告记录，未远程重验。

## 当前 v2 的实现边界

| 实现/模板 | 已核对事实 | 未获本归档支持的结论 |
|---|---|---|
| `ara_trajectory.py`，`capture_trajectory_io` / `build_trajectory_bank` / `trajectory_loss` | 基座续写上的 teacher forcing、同一步损失；仍是冻结轨迹代理 | 不能证明编辑模型实际生成轨迹已保持或拒答下降 |
| `ara_search.py`，`sample_v2_parameters`；`study_runner.py`，`prepare_trajectory_study` / `run_trajectory_study` | 八坐标、单 worker 的 8 锚点 + 24 随机 + 88 TPE 尝试 | 不能与 v1 六坐标结果作有控制的效果排序 |
| `scorers/refusal_log_odds.py`，`RefusalLogOdds` | 连续前缀对数优势评分 | 不能替代语义评判或保证能力保持 |
| `acceptance.py`、`acceptance_export.py`、`artifact_schema.py`、`protocol_data.py` | 候选筛选、重复重放、审计隔离、指纹绑定和暂存发布 | 没有本次成功候选或已通过的 v2 审计可报告 |
| `config.qwen38-27b-cara-v2.toml` | 每类 96 提示、至多 8 续写位置、衰减 0.85、部署增益；Gram 项与 v1 初始 Gram 归一化项不同 | 配置存在不代表效果实验已经执行 |

v1 的 `model_commit` 为 null。v2 模板的模型 revision 不回填到 v1；模型指纹
也不等于可独立恢复的权重快照。软件包版本未记录，保留缺失。
2 小时 38 分钟来自日志；52.05 GB GPU allocation 和约 5.47 GB resident
memory 是终端采样值，不是全程峰值，沿用原单位且不与 reserved memory 混同。

## 文献与声明账本

沿用原参考文献的结果和适用范围，本次不增加外部文献实测数字，也不把
已有文献结果移植成本地模型结果。新增 BibTeX 仅为真实本地源码及归档工件，
不虚构 DOI、会场或公开归档地址。历史文献核验保存在
`literature-provenance.md`；本次新增 C026–C036 并为历史 C001–C025
补充版本和章节定位。E1–E5 沿用原定义，E6 专指本地归档观察。

## 最终评审勘误（节点 #3）

Scanner、Reviewer 与 QA 分别核对上游逐行归一化式，确认保护分支中
行范数为 `s_j * ||w_j|| / epsilon`，一般只能写为不大于 `s_j`；严格
小于要求 `s_j > 0`。初始零行使两者均为零。已修正当前正文
`sections/03-method.tex` 的措辞，不改变公式、算法或实验统计。
历史 `paper-output/analysis/math-formulation.md` 中对应的严格不等式
保留为旧稿记录，其边界情况以本勘误和修订正文为准。
