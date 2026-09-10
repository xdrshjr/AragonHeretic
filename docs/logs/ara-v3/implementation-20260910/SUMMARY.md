# ARA v3 代码节点交付记录

本轮按规格 v2.0 完成 `sequential-v3` 的代码实现与 P0 验证，交接代码评审节点。没有执行 Git 暂存或提交，也没有改动既有论文暂存删除。研究目标尚未通过实验验收。

## 实现内容

- 独立协议准备与模型加载前检查：固定数据边界、角色计数、内容/情景去重、审计历史、源码/依赖/模型/tokenizer/生成身份。
- 顺序重捕获与全基座保持参考：绝对因子替换、组内事务、真实前向与 monitor 门槛、失败恢复、固定/刷新/局部保持/冻结诊断分支。
- 序列 KL、双轴语义标签、情景组计数、Clopper–Pearson 上界及能力配对 bootstrap。
- 24-attempt 独立搜索、固定前三名筛选、候选锁、快照重放、跨重启累计预算、阶段与成员 watchdog。
- staging 核心文件绑定、独立 PEFT 重载探针、完整集合冻结、逐成员一次审计账本、离线 finalize 与分级提升。缺失证据不会变成研究通过。
- 新双卡配置、准备入口和 Linux 阶段脚本；保留旧数值求解路径和通用接口的兼容分发。

详细接口补充与实施选择记录在规格末尾“实施过程发现的方案缺陷”。交付文件及 SHA-256 见 [static-validation.json](static-validation.json)。

## 实际验证

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 全部 `unittest discover -s tests -p test_*.py` | **165 项通过，1.669 秒，退出码 0** | [unittest.log](unittest.log) |
| 34 个任务相关 Python 文件的 Ruff/语法检查 | 通过 | [static-validation.json](static-validation.json) |
| 新增源码：800 行文件、50 行函数、5 参数、复杂度 10 上限 | 通过 | [static-validation.json](static-validation.json) |
| Linux `bash -n`、准备及研究 CLI 帮助 | 通过 | [entrypoints.log](entrypoints.log) |
| 缺少冻结协议时的 preflight | 退出码 2，没有创建 study | [entrypoints.log](entrypoints.log) |
| v3 复现参数或核心文件被修改 | 测试确认拒绝通过 | `test_v3_artifact_chain_rejects_parameter_and_core_tampering` |

验证覆盖固定 train=416 的越界拒绝、合法 100 行切分、跨角色内容泄漏、因果位置与 EOS、快照绝对恢复、真实文件消费/恢复、KL 方向、缺标注分母、100/300 条零拒答区间、能力差值方向、重放续跑和失败锁、审计预算缺项，以及离线 finalize 不调用模型。

远端环境：开发服务器 Ubuntu、Python 3.12，torch 2.13.0、transformers 5.16.1、peft 0.20.0、optuna 4.9.0、lm_eval 0.4.12。测试在 `/home/xdrshjr/.cache/ara-v3-code-check-ff8102e2` 隔离目录运行；没有覆盖 `/home/xdrshjr/work/AragonHeretic`。本地执行静态检查，远端只运行 CPU 数值/事务测试与入口检查。

前一轮全量测试因隔离同步遗漏旧 `smoke-prompts.txt` 夹具而出现一项错误，补齐后通过；已在任务运行日志保留该失败及后续通过记录。能力任务加载接口按远端已安装源码核对后改为执行冻结 YAML 路径。测试中的人工构造响应、标签、能力记录和候选仅是断言夹具，不是研究数据。

## 尚未验证的研究门槛

本轮未下载或加载真实研究模型，未执行 27B 双 3090 NF4 pilot、288-attempt 矩阵、跨模型机制复验、真实能力任务或人工双评。没有生成正式研究 protocol、独立审计题目、成功 adapter 或效果分数。

后续须先准备实际缓存及文件身份、独立审计来源/抽样前提、可执行的逐题能力 YAML、判断器与模板摘要、阶段预算和标注人员。P1 实测双卡 placement、显存/RSS 峰值及耗时后，才能决定是否进入正式矩阵。现有 `tests/README.md` 的下载 tiny-random 输出 checksum 流程本轮未运行；新增 `model.py` 包装接口没有改动旧初始化或生成实现。

因此，本记录支持“代码实现已交付并通过所列 P0 检查”，不能支持“已实现极低拒答率”或“已完成研究”。
