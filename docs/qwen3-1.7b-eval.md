# Qwen3-1.7B 独立 Adapter 评估

训练入口是 `run_1.7b_qwen3_latest_ara.sh`，评估入口是
`eval_1.7b_qwen3_latest_ara.sh`。评估读取导出的 Adapter，不进行训练。

## 直接运行

```bash
conda activate aragon-heretic
cd ~/work/AragonHeretic
./eval_1.7b_qwen3_latest_ara.sh --offline
```

脚本已填入本次成功训练的 Adapter 地址，默认 GPU 1、每侧 20 条数据。
可修改脚本顶部的变量，也可以通过参数覆盖：

```bash
./eval_1.7b_qwen3_latest_ara.sh \
  --adapter /home/xdrshjr/heretic-runs/qwen3-1.7b/ara-engineering-S2-42-20260917T053956Z/artifacts/best-adapter \
  --eval-samples 100 \
  --gpu 1 --offline
```

- `ADAPTER` / `--adapter`：包含 `adapter_config.json` 和
  `adapter_model.safetensors` 的目录。当前支持本项目导出的普通 ARA LoRA。
- `EVAL_SAMPLES` / `--eval-samples`：**每侧**数量；100 表示 100 条 good
  和 100 条 bad。原模型和 Adapter 使用完全相同的题目。
- `START_INDEX` / `--start-index`：从 0 开始，默认 `auto`。通过 Adapter
  所属运行的 `engineering-config.json`，跳过 fit、monitor、development
  使用的全部行。本次训练默认使用前 36 行，因此评估从第 36 行开始。
- 起点加每侧数量不能超过 400；本次默认起点下最多评估每侧 364 条。
- 如果复制或移动 Adapter 后没有原运行配置，请显式设置 `--start-index`。
  来源未知时结果中的数据重叠状态是 null，不能据此宣称数据独立。
- `--output-dir`：指定空输出目录。默认自动在
  项目目录下的 `eval-runs/qwen3-1.7b/` 创建新的评估目录。
- `--dry-run`：只验证路径和数据范围，不加载模型、不创建输出目录。

也支持环境变量：`ARA_EVAL_ADAPTER`、`ARA_EVAL_SAMPLES`、
`ARA_EVAL_START_INDEX`、`ARA_EVAL_OUTPUT_ROOT`。命令行参数优先。

## 复核原训练结果

本次训练的 development 分区是 `[16:36]`，可明确选择相同分区：

```bash
./eval_1.7b_qwen3_latest_ara.sh \
  --start-index 16 --eval-samples 20 --offline
```

这属于原开发集上的复核；输出将标记与来源训练运行数据重叠。
默认 `auto` 使用另一批题目，指标无需与原来 95% → 65% 完全相同。

## 评估产物

- `eval-result.json`：Adapter 路径与 SHA256、实际数据区间、基线和 Adapter
  拒答关键词率、百分点下降、首 token KL、序列 KL，以及评估状态。
- `eval-evidence.json`：`responses` 和 `baseline_responses` 中每条均包含
  `question`（实际输入的问题）、`system_prompt`、`text`（模型回答）、
  题目 ID、生成有效长度和评分身份。`inputs.good` 和 `inputs.bad`
  保存两侧全部输入；good 侧用于 KL 评估，bad 侧用于回答对比。
- `eval-protocol.json`：数据身份、生成设置和模型身份。

关键词率越低表示包含拒答标记的回答越少，不等于回答更正确或更有用。
KL 使用 good 侧原模型生成的固定序列衡量输出分布偏移；0.15 是现有
工程入口的参考阈值，不能代替通用能力测试。脚本没有进行正式研究验收。

## 96 服务器验证记录（2026-09-17）

- 8 项单元/微型模型集成测试通过；Bash 语法、Ruff 检查通过。
- 参数覆盖、100 条每侧 dry-run、越界拒绝验证通过。
- 实际 GPU 1 评估使用默认每侧 20 条、区间 `[36:56]`，退出码 0。
- 基线关键词率 95%，Adapter 90%，下降 5 个百分点；首 token KL
  0.065953、序列 KL 0.023110。这是另一批数据，不等于原开发集结果。
- 服务器结果目录：
  `/home/xdrshjr/heretic-evals/qwen3-1.7b/validation-20260917T062046Z/`。
