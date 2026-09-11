# Pro 6000 一键全量实验准备记录

2026-09-11 已完成一键入口部署和真实全量预检，后台准备进程退出码为 0，
状态为 `ready`。**未启动 24 次全量 GPU 搜索，也未消费 GPU 运行预算。**

## 可直接运行的命令

```bash
bash /root/autodl-fs/AragonHeretic-v3/scripts/run_qwen38_27b_ara_v3_pro6000.sh
```

以上命令创建新实验并后台启动。若希望直接使用本次已经准备好的目录：

```bash
bash /root/autodl-fs/AragonHeretic-v3/scripts/run_qwen38_27b_ara_v3_pro6000.sh --resume /autodl-fs/data/heretic-runs/ara-v3-pro6000-S2-42-20260911T085214Z-59bcea
```

两条命令择一执行即可。默认是 S2、seed 42、24 次搜索、最多 2 轮，
累计单卡时间上限 48 小时。全量耗时尚未实测，48 小时不是完成时间预测。

## 真实验证

| 检查 | 结果 |
| --- | --- |
| 服务器 | `connect.westd.seetacloud.com:46241` |
| GPU | RTX PRO 6000 Blackwell Server Edition，97,887 MiB |
| 预检完成时间 | 2026-09-11 08:56:49 UTC |
| 准备进程退出码 | 0 |
| 模型与 tokenizer | 实际缓存文件逐个 SHA-256 校验通过 |
| 全量样本 | 每侧 fit 候选 192 / 选中 96，monitor 64，mechanism 44，development 100 |
| 角色隔离 | 全部 800 条正文规范化摘要互不重复 |
| 保留题目 | development 与 mechanism-development 的正文及系统提示摘要与 pilot 完全相同 |
| 准备恢复 | 在已存在协议上再次准备，协议文件 SHA-256 不变 |
| 研究入口范围检查 | 拒绝将本实验协议直接用于正式研究入口 |
| 磁盘 | 准备时可用 36,575,444,992 字节，预留需求 33,218,887,680 字节 |
| 收尾 GPU 占用 | 0 MiB |

一键运行从已提交 Git 源码归档，冻结本次代码为
`95dc9d854aee47ff71810b0b24ad74e4b0a7d907`。后续说明、验证记录及启动提示的
格式整理不会改写本目录的冻结源码。

协议摘要：
`878eac051639f28a7bf0e05bd9768ba462b77207a1b10c9b569c1c347663a5fb`。
完整校验见 [validation.json](validation.json)、[prepared.json](prepared.json)
和 [实际运行配置](config.toml)。

## 测试

服务器环境运行 56 项相关单元与回归测试，全部通过：

```bash
python -m unittest test_pro6000_experiment test_ara_research_runner test_ara_refinement_capture test_ara_refinement test_ara_research_acceptance -v
```

新增入口覆盖 13 项测试，包括全量划分、冻结预算、源码归档、单 GPU 计费、
24 次原生随机/TPE 调度、恢复不重跑、重复进程锁及导出文件损坏检查。
参见 [unit-tests.log](unit-tests.log)。

另用真实 PEFT 小模型完成适配器导出、离线重载、选中因子一致性和原模型恢复测试，
1 项通过，参见 [adapter-export-test.log](adapter-export-test.log)。
Python 格式与 Ruff 静态检查通过，Bash 语法检查通过。

本次覆盖真实全量数据准备、模型文件预检和入口功能验证；27B 模型实际执行证据
来自此前[已完成的小样本 pilot](../pilot-20260911-pro6000/SUMMARY.md)。
全量 96/64 规格的求解耗时、峰值资源和效果尚待用户启动实验后记录。
该入口只输出开发集实验与对照适配器，研究验收状态保持 `not_run`。
