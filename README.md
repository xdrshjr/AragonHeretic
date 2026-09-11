<img width="128" align="right" alt="Logo" src="https://github.com/user-attachments/assets/df5f2840-2f92-4991-aa57-252747d7182e" />

# Heretic: Fully automatic censorship removal for language models<br><br>[![Discord](https://img.shields.io/discord/1447831134212984903?color=5865F2&label=discord&labelColor=black&logo=discord&logoColor=white&style=for-the-badge)](https://discord.gg/gdXc48gSyT) [![Matrix](https://img.shields.io/badge/Matrix-black?logo=matrix&style=for-the-badge)](https://matrix.to/#/#heretic:matrix.org) [![Follow us on Hugging Face](https://huggingface.co/datasets/huggingface/badges/resolve/main/follow-us-on-hf-md-dark.svg)](https://huggingface.co/heretic-org) [![Codeberg mirror](https://img.shields.io/badge/Codeberg%20mirror-black?logo=codeberg&style=for-the-badge)](https://codeberg.org/p-e-w/heretic)

[![#1 Repository of the Day](https://trendshift.io/api/badge/repositories/20538)](https://trendshift.io/repositories/20538)

Heretic is a tool that removes censorship (aka "safety alignment") from
transformer-based language models without expensive post-training.
It combines an advanced implementation of directional ablation, also known
as "abliteration" ([Arditi et al. 2024](https://arxiv.org/abs/2406.11717),
Lai 2025 ([1](https://huggingface.co/blog/grimjim/projected-abliteration),
[2](https://huggingface.co/blog/grimjim/norm-preserving-biprojected-abliteration))),
with a TPE-based parameter optimizer powered by [Optuna](https://optuna.org/).

This approach enables Heretic to work **completely automatically.** Heretic
finds high-quality abliteration parameters by co-minimizing the number of
refusals and the KL divergence from the original model. This results in a
decensored model that retains as much of the original model's intelligence
as possible. Using Heretic does not require an understanding of transformer
internals. In fact, anyone who knows how to run a command-line program
can use Heretic to decensor language models.

Heretic supports most dense models, including many multimodal models,
several different MoE architectures, and even some hybrid models like Qwen3.5.
Pure state-space models and certain other research architectures are not yet
supported out of the box.

<img width="650" height="715" alt="Screenshot" src="https://github.com/user-attachments/assets/d71a5efa-d6be-4705-a817-63332afb2d15" />

&nbsp;

Running unsupervised with the default configuration, Heretic can produce
decensored models that rival the quality of abliterations created manually
by human experts:

| Model | Refusals for "harmful" prompts | KL divergence from original model for "harmless" prompts |
| :--- | ---: | ---: |
| [google/gemma-3-12b-it](https://huggingface.co/google/gemma-3-12b-it) (original) | 97/100 | 0 *(by definition)* |
| [mlabonne/gemma-3-12b-it-abliterated-v2](https://huggingface.co/mlabonne/gemma-3-12b-it-abliterated-v2) | 3/100 | 1.04 |
| [huihui-ai/gemma-3-12b-it-abliterated](https://huggingface.co/huihui-ai/gemma-3-12b-it-abliterated) | 3/100 | 0.45 |
| **[p-e-w/gemma-3-12b-it-heretic](https://huggingface.co/p-e-w/gemma-3-12b-it-heretic) (ours)** | **3/100** | **0.16** |

The Heretic version, generated without any human effort, achieves the same
level of refusal suppression as other abliterations, but at a much lower
KL divergence, indicating less damage to the original model's capabilities.
*(You can reproduce those numbers using Heretic's built-in evaluation functionality,
e.g. `heretic --model google/gemma-3-12b-it --evaluate-model p-e-w/gemma-3-12b-it-heretic`.
Note that the exact values might be platform- and hardware-dependent.
The table above was compiled using PyTorch 2.8 on an RTX 5090.)*

Of course, mathematical metrics and automated benchmarks never tell the whole
story, and are no substitute for human evaluation. Models generated with
Heretic have been well-received by users (links and emphasis added):

> "I was skeptical before, but I just downloaded
> [**GPT-OSS 20B Heretic**](https://huggingface.co/p-e-w/gpt-oss-20b-heretic)
> model and holy shit. It gives properly formatted long responses to sensitive topics,
> using the exact uncensored words that you would expect from an uncensored model,
> produces markdown format tables with details and whatnot. Looks like this is
> the best abliterated version of this model so far..."
> [*(Link to comment)*](https://old.reddit.com/r/LocalLLaMA/comments/1oymku1/heretic_fully_automatic_censorship_removal_for/np6tba6/)

> "[**Heretic GPT 20b**](https://huggingface.co/p-e-w/gpt-oss-20b-heretic)
> seems to be the best uncensored model I have tried yet. It doesn't destroy a
> the model's intelligence and it is answering prompts normally would be
> rejected by the base model."
> [*(Link to comment)*](https://old.reddit.com/r/LocalLLaMA/comments/1oymku1/heretic_fully_automatic_censorship_removal_for/npe9jng/)

> "[[**Qwen3-4B-Instruct-2507-heretic**](https://huggingface.co/p-e-w/Qwen3-4B-Instruct-2507-heretic)]
> Has been the best unquantized abliterated model that I have been able to run on 16gb vram."
> [*(Link to comment)*](https://old.reddit.com/r/LocalLLaMA/comments/1phjxca/im_calling_these_people_out_right_now/nt06tji/)

Heretic models have also been independently benchmarked using standard metrics
like MMLU and GSM8K, and have been found to compare favorably with models
produced by competing abliteration tools:
[1](https://old.reddit.com/r/LocalLLaMA/comments/1sojjoc/abliterlitics_benchmark_and_tensor_analysis/),
[2](https://old.reddit.com/r/LocalLLaMA/comments/1sy18lx/abliterlitics_benchmarks_and_tensor_comparison/).

The community has created and published
[well over 5000](https://huggingface.co/models?other=heretic)
models with Heretic.


## 栀夏界面截图

![栀夏聊天界面截图](docs/images/zhixia-chat-screenshot.png)


## Usage

### Pro 6000：一键运行最新 Heretic-ARA v3

已准备好的 Pro 6000 服务器使用以下命令启动全量 S2 实验：

```bash
bash /root/autodl-fs/AragonHeretic-v3/scripts/run_qwen38_27b_ara_v3_pro6000.sh
```

入口自动准备数据与冻结配置，后台执行 **24 次搜索（4 个锚点、8 次随机、12 次 TPE）**，
并保存快照、指标和最佳开发集候选的 LoRA 适配器。
默认 Qwen3.8-27B、NF4、rank 128、seed 42、S2 顺序刷新、最多 2 轮；
每侧从 192 条 fit 候选中选 96 条，monitor 64 条，development 100 条。
默认累计单卡时间上限为 48 小时，可在新实验启动时用 `--hours` 调整；
这是预算上限，不是完成时间预测。SSH 断开后继续运行。

启动后终端会打印运行目录和日志位置。用 `--status <运行目录>` 查看进度，
用 `--resume <运行目录>` 继续已准备或中断的实验。
完整说明见 [Pro 6000 实验操作手册](docs/operations/pro6000-experiments.md)。
本入口记录开发集实验结果；论文所需的独立审计与语义评审仍需另行完成。

### 通用安装与运行

Prepare a Python 3.10+ environment with PyTorch 2.2+ installed as appropriate
for your hardware. Then run:

```sh
pip install -U heretic-llm
heretic Qwen/Qwen3-4B-Instruct-2507
```

Replace `Qwen/Qwen3-4B-Instruct-2507` with whatever model you want to decensor.

> [!IMPORTANT]
>
> While PyTorch 2.2 is the minimum version of PyTorch needed for Heretic to work,
> some models and configurations might require features only found in
> later versions. For example, loading MXFP4-quantized models like gpt-oss
> uses `torch.accelerator`, which was added in PyTorch 2.6.

> [!TIP]
>
> Heretic uses [uv](https://docs.astral.sh/uv/) for dependency management,
> and the repository includes a `uv.lock` file pinning every package version.
> If you already use uv (and you probably should!), you can just clone the repo
> and run Heretic with `uv run heretic`, which ensures that your dependencies
> match those used by the developers, improving reliability and security.

The process is fully automatic and does not require configuration; however,
Heretic has a variety of configuration parameters that can be changed for
greater control. Run `heretic --help` to see available command-line options,
or look at [`config.default.toml`](config.default.toml) if you prefer to use
a configuration file.

At the start of a program run, Heretic benchmarks the system to determine
the optimal batch size to make the most of the available hardware.
On an RTX 3090, with the default configuration, decensoring
[Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507)
takes about 20-30 minutes. Note that Heretic supports model quantization with
bitsandbytes, which can drastically reduce the amount of VRAM required to process
models. Set the `quantization` option to `bnb_4bit` to enable quantization.

After Heretic has finished decensoring a model, you are given the option to
save the model, upload it to Hugging Face, chat with it to test how well it works,
run standard benchmarks on it, or any combination of those actions.

### Calibrated Arbitrary-Rank Ablation (CARA)

The default `directional` method is unchanged. For models whose refusal
behavior is not well represented by one residual direction, Heretic also
provides `abliteration_method = "ara"`. CARA captures clean module input/output
pairs for harmless and harmful calibration prompts, then optimizes a bounded,
scale-normalized objective directly in a high-rank LoRA adapter. The base model
remains frozen, including when it is loaded with bitsandbytes NF4 quantization.

A pinned Qwen3.8-27B research baseline is provided in
[`config.qwen38-27b-cara.toml`](config.qwen38-27b-cara.toml). Copy it to
`config.toml`, review its paths, and run:

```sh
cp config.qwen38-27b-cara.toml config.toml
uv run heretic
```

The baseline targets two 24 GiB GPUs using `device_map = "balanced"`, a
22 GiB per-device cap, NF4, rank 128, and capture batch size 1. It separates
calibration (`train[:300]`), validation (`train[300:400]`), and the one-time
audit (`test[:100]`) at fixed dataset revisions. A candidate is exportable only
after deterministic replay, the configured Keywords/KL gate, and a clean-process
adapter reload all pass; `acceptance.json` is the machine-readable result.

CARA is still a local linear proxy: it does not prove a global causal mechanism,
semantic safety, multilingual coverage, or preservation of visual, long-context,
coding, and thinking-mode behavior. Lower keyword rate is not equivalent to a
correct or safe answer. The Qwen baseline therefore exports an adapter only and
does not upload it automatically; a BF16 merge requires a separate memory check
and full re-evaluation.


## Research features

In addition to its primary function of removing model censorship, Heretic also
provides features designed to support research into the semantics of model internals
(interpretability). To use those features, you need to install Heretic with the
optional `research` extra:

```sh
pip install -U 'heretic-llm[research]'
```

This gives you access to the following functionality:

### Generate plots of residual vectors by passing `--plot-residuals`

When run with this flag, Heretic will:

1. Compute residual vectors (hidden states) for the first output token,
   for each transformer layer, for both "harmful" and "harmless" prompts.
2. Perform a [PaCMAP projection](https://github.com/YingfanWang/PaCMAP)
   from residual space to 2D-space.
3. Left-right align the projections of "harmful"/"harmless" residuals
   by their geometric medians to make projections for consecutive layers
   more similar. Additionally, PaCMAP is initialized with the previous
   layer's projections for each new layer, minimizing disruptive transitions.
4. Scatter-plot the projections, generating a PNG image for each layer.
5. Generate an animation showing how residuals transform between layers,
   as an animated GIF.

<img width="800" height="600" alt="Plot of residual vectors" src="https://github.com/user-attachments/assets/981aa6ed-5ab9-48f0-9abf-2b1a2c430295" />

See [the configuration file](config.default.toml) for options that allow you
to control various aspects of the generated plots.

Note that PaCMAP is an expensive operation that is performed on the CPU.
For larger models, it can take an hour or more to compute projections
for all layers.

### Print details about residual geometry by passing `--print-residual-geometry`

If you are interested in a quantitative analysis of how residual vectors
for "harmful" and "harmless" prompts relate to each other, this flag gives you
the following table, packed with metrics that can facilitate understanding
the same (for [gemma-3-270m-it](https://huggingface.co/google/gemma-3-270m-it)
in this case):

```
┏━━━━━━━┳━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━┓
┃ Layer ┃ S(g,b) ┃ S(g*,b*) ┃  S(g,r) ┃ S(g*,r*) ┃  S(b,r) ┃ S(b*,r*) ┃      |g| ┃     |g*| ┃      |b| ┃     |b*| ┃     |r| ┃    |r*| ┃   Silh ┃
┡━━━━━━━╇━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━┩
│     1 │ 1.0000 │   1.0000 │ -0.4311 │  -0.4906 │ -0.4254 │  -0.4847 │   170.29 │   170.49 │   169.78 │   169.85 │    1.19 │    1.31 │ 0.0480 │
│     2 │ 1.0000 │   1.0000 │  0.4297 │   0.4465 │  0.4365 │   0.4524 │   768.55 │   768.77 │   771.32 │   771.36 │    6.39 │    5.76 │ 0.0745 │
│     3 │ 0.9999 │   1.0000 │ -0.5699 │  -0.5577 │ -0.5614 │  -0.5498 │  1020.98 │  1021.13 │  1013.80 │  1014.71 │   12.70 │   11.60 │ 0.0920 │
│     4 │ 0.9999 │   1.0000 │  0.6582 │   0.6553 │  0.6659 │   0.6627 │  1356.39 │  1356.20 │  1368.71 │  1367.95 │   18.62 │   17.84 │ 0.0957 │
│     5 │ 0.9987 │   0.9990 │ -0.6880 │  -0.6761 │ -0.6497 │  -0.6418 │   766.54 │   762.25 │   731.75 │   732.42 │   51.97 │   45.24 │ 0.1018 │
│     6 │ 0.9998 │   0.9998 │ -0.1983 │  -0.2312 │ -0.1811 │  -0.2141 │  2417.35 │  2421.08 │  2409.18 │  2411.40 │   43.06 │   43.47 │ 0.0900 │
│     7 │ 0.9998 │   0.9997 │ -0.5258 │  -0.5746 │ -0.5072 │  -0.5560 │  3444.92 │  3474.99 │  3400.01 │  3421.63 │   86.94 │   94.38 │ 0.0492 │
│     8 │ 0.9990 │   0.9991 │  0.8235 │   0.8312 │  0.8479 │   0.8542 │  4596.54 │  4615.62 │  4918.32 │  4934.20 │  384.87 │  377.87 │ 0.2278 │
│     9 │ 0.9992 │   0.9992 │  0.5335 │   0.5441 │  0.5678 │   0.5780 │  5322.30 │  5316.96 │  5468.65 │  5466.98 │  265.68 │  267.28 │ 0.1318 │
│    10 │ 0.9974 │   0.9973 │  0.8189 │   0.8250 │  0.8579 │   0.8644 │  5328.81 │  5325.63 │  5953.35 │  5985.15 │  743.95 │  779.74 │ 0.2863 │
│    11 │ 0.9977 │   0.9978 │  0.4262 │   0.4045 │  0.4862 │   0.4645 │  9644.02 │  9674.06 │  9983.47 │  9990.28 │  743.28 │  726.99 │ 0.1576 │
│    12 │ 0.9904 │   0.9907 │  0.4384 │   0.4077 │  0.5586 │   0.5283 │ 10257.40 │ 10368.50 │ 11114.51 │ 11151.21 │ 1711.18 │ 1664.69 │ 0.1890 │
│    13 │ 0.9867 │   0.9874 │  0.4007 │   0.3680 │  0.5444 │   0.5103 │ 12305.12 │ 12423.75 │ 13440.31 │ 13432.47 │ 2386.43 │ 2282.47 │ 0.1293 │
│    14 │ 0.9921 │   0.9922 │  0.3198 │   0.2682 │  0.4364 │   0.3859 │ 16929.16 │ 17080.37 │ 17826.97 │ 17836.03 │ 2365.23 │ 2301.87 │ 0.1282 │
│    15 │ 0.9846 │   0.9850 │  0.1198 │   0.0963 │  0.2913 │   0.2663 │ 16858.58 │ 16949.44 │ 17496.00 │ 17502.88 │ 3077.08 │ 3029.60 │ 0.1611 │
│    16 │ 0.9686 │   0.9689 │ -0.0029 │  -0.0254 │  0.2457 │   0.2226 │ 18912.77 │ 19074.86 │ 19510.56 │ 19559.62 │ 4848.35 │ 4839.75 │ 0.1516 │
│    17 │ 0.9782 │   0.9784 │ -0.0174 │  -0.0381 │  0.1908 │   0.1694 │ 27098.09 │ 27273.00 │ 27601.12 │ 27653.12 │ 5738.19 │ 5724.21 │ 0.1641 │
│    18 │ 0.9184 │   0.9196 │  0.1343 │   0.1430 │  0.5155 │   0.5204 │   190.16 │   190.35 │   219.91 │   220.62 │   87.82 │   87.59 │ 0.1855 │
└───────┴────────┴──────────┴─────────┴──────────┴─────────┴──────────┴──────────┴──────────┴──────────┴──────────┴─────────┴─────────┴────────┘
g = mean of residual vectors for good prompts
g* = geometric median of residual vectors for good prompts
b = mean of residual vectors for bad prompts
b* = geometric median of residual vectors for bad prompts
r = residual direction for means (i.e., b - g)
r* = residual direction for geometric medians (i.e., b* - g*)
S(x,y) = cosine similarity of x and y
|x| = L2 norm of x
Silh = Mean silhouette coefficient of residuals for good/bad clusters
```


### Experimental CARA trajectory-v2 protocol

历史提醒：该 v2 模板的固定 harmful revision 只有 416 条 train 数据，
原 `train[400:500]` 不满足 100 条验证要求，预检会明确拒绝。
已有 v1 归档的最佳 Keywords 为 0.54，不能作为方法成功的证据。

### ARA sequential-v3 研究协议

`sequential-v3` 在每个层组更新前重新捕获当前输入，并使用同一 token
序列上的全基座输出作为保持参考。组内因子按绝对值替换，真实联合前向与
monitor 同时通过后才接受。默认 directional 及既有 point-v1/trajectory-v2
数值求解路径保留。

双 3090 模板为 `config.qwen38-27b-cara-v3-96.toml`，使用 NF4、每卡
22 GiB 加载预算以及独立的 4+8+12 搜索。模板中的模型缓存路径需实测。
运行前必须准备不可变的数据、源码、模型及 tokenizer 身份清单，并冻结
生成设置、独立审计历史、能力任务和阶段预算；缺项会在模型分配前报错。
外部研究审计题目和人工评审资源须由独立准备者提供。

```sh
python scripts/prepare_ara_research_protocol.py --config preparation.json
bash scripts/run_ara_research_96.sh --config config.qwen38-27b-cara-v3-96.toml --run-dir docs/logs/ara-v3/S2-42 --phase preflight
bash scripts/run_ara_research_96.sh --config config.qwen38-27b-cara-v3-96.toml --run-dir docs/logs/ara-v3/S2-42 --phase search
```

准备 JSON 的结构见 [v3 规格](docs/plans/ara-v2-refusal-optimization/spec.md)。
每份角色源文件包含 `rows` 数组；每题固定 `prompt_id`、`scenario_group_id`、
`primary_in_group`、`source`、`revision`、`row_index`、`language`、`category`、
`system` 与 `text`，源文件配置包含 `path`、`sha256` 和已核对的 `license`。
HF 源另提供 `dataset_spec` 和逐题 `split`。`source_files` 映射实际源码
相对路径到 SHA-256，`source_tree_hash` 为该映射规范 JSON 的 SHA-256。
`package_versions` 必须包含 torch、transformers、peft、optuna、lm_eval
等实际运行版本；训练与审计预检会逐项核对。
fit 全池 192 条中按 seed 选择 96 条；monitor/机制开发/development
分别为每侧 64/44/100 条，审计正文不会由训练加载器读取。

正式搜索须先通过单独 pilot；pilot 使用另一份 `pilot=true` 协议，fit 和
monitor 每侧各 8 条，且禁止正式提升。`phase_budgets.search.pilot_evidence`
绑定真实 pilot 汇总的 `path`/`sha256`，汇总包含 `status` 和
`predicted_slowest_study_seconds`，加 25% 余量后不得超过 8 小时。
pilot、ablation、audit 的预算包含 `max_wallclock_seconds`、`max_gpu_hours`
及 `members`；audit 还固定 `annotation_deadline`、
`member_budgets.<member_id>.max_wallclock_seconds` 及
`annotations.roles`/`annotations.record_count`。恢复、开发语义评估和重放
共享累计预算，硬超时留下中断证据并退出。

每个 search 产生锁定候选或失败对照的 `member.json`。所有方法和 seed
完成后，汇总到包含 `members` 数组的集合文件；没有可用结果的成员必须
显式记录 `availability=unavailable` 和原因。加入 B0 共享基座后执行：

```sh
bash scripts/run_ara_research_96.sh --config config.qwen38-27b-cara-v3-96.toml --run-dir docs/logs/ara-v3/evaluation --phase freeze --members members.json
bash scripts/run_ara_research_96.sh --config config.qwen38-27b-cara-v3-96.toml --run-dir docs/logs/ara-v3/evaluation --phase audit --evaluation-plan docs/logs/ara-v3/evaluation/evaluation-plan.json
bash scripts/run_ara_research_96.sh --config config.qwen38-27b-cara-v3-96.toml --run-dir docs/logs/ara-v3/evaluation --phase finalize --evaluation-plan docs/logs/ara-v3/evaluation/evaluation-plan.json --labels labels.json
```

`labels.json` 按成员 ID、good/bad 侧保存与响应 hash 绑定的双人独立标注；
分歧须有第三人裁决。开发自动判定器在 `judge_identity.development` 中固定
命令、源码 hash 与超时，不能替代审计人工双评。能力任务采用冻结的
MMLU/GSM8K/IFEval 题目、文档 hash 和逐题二元正确性，分组配对 bootstrap。
缺标注或统计前提不足返回 `inconclusive`，比较制品永久没有正式提升资格。

`generation_profiles` 固定 `fit=8`、`keywords=100`、`semantic=256`、
`sequence_kl=32`，其中 `chat_template_kwargs` 和 `generation_kwargs`
须与运行 TOML 完全一致。能力 `task_config_path` 指向实际执行的 YAML，
`task_config_hash` 校验该文件；`task_id` 是返回逐题 samples 的单一任务名。
MMLU 多学科题目须在准备阶段冻结为该清单；不能用组名代替逐题身份。
配置引用的模板/判断器也须加入冻结源码清单，运行版本记入协议。

A1/A2/F1 在 `ara_v3.parent_candidate_lock_path` 与对应 hash 中指定
S2/S1 父候选，仅应用父参数并执行机制开发对照，使用单独消融预算。
v3 复现须使用匹配的 v3 TOML 和正式目录内的 `reproduce.json`；
该路径校验完整产物链、重载绝对因子并运行非审计探针，不启动新搜索。

退出码：0 为所请求阶段完成；2 为协议/身份错误；3 为运行或预算失败；
4 为目标明确失败；5 为证据不足。只有 `finalize` 的 0 表示预注册研究目标
通过。代码测试通过不表示新方法已经达到极低拒答率；真实 27B pilot、
完整矩阵和独立审计的结论须以运行归档为准。

### trajectory-v2 原协议说明

`config.qwen38-27b-cara-v2.toml` defines the pre-registered, single-worker
trajectory-v2 experiment for Qwen3.8-27B. The protocol captures the base
model's first eight continuation positions, optimizes step-aligned local CARA
updates, and searches a fixed eight-dimensional space using eight anchors,
24 random startup attempts, and 88 constrained TPE attempts.

Run it on the designated single-GPU environment with:

```console
bash scripts/run_qwen38_27b_cara_v2.sh
```

The final audit is not loaded until a validation candidate has been locked and
written to a staging adapter. A fresh process consumes that audit once. Success
requires all validation, replay, audit, hash, and adapter post-checks; any gate
failure exits nonzero and does not create the formal adapter directory.

This is an experimental interpretability and model-behavior intervention. A
lower refusal-keyword rate is not evidence that answers are correct, safe, or
capability-preserving, and it must not be presented as a general safety result.

## How Heretic works

Heretic implements a parametrized variant of directional ablation. For each
supported transformer component (currently, attention out-projection and
MLP down-projection), it identifies the associated matrices in each transformer
layer, and orthogonalizes them with respect to the relevant "residual direction",
inhibiting the expression of that direction in the result of multiplications
with that matrix.

Residual directions are computed for each layer as a difference-of-means between
the first-token residuals for "harmful" and "harmless" example prompts.

The ablation process is controlled by several optimizable parameters:

* `direction_index`: Either the index of a residual direction, or the special
  value `per layer`, indicating that each layer should be ablated using the
  residual direction associated with that layer.
* `max_weight`, `max_weight_position`, `min_weight`, and `min_weight_distance`:
  For each component, these parameters describe the shape and position of the
  ablation weight kernel over the layers. The following diagram illustrates this:

<img width="800" height="500" alt="Explanation" src="https://github.com/user-attachments/assets/82e4b84e-5a82-4faf-b918-ac642f9e4892" />

&nbsp;

Heretic's main innovations over existing abliteration systems are:

* The shape of the ablation weight kernel is highly flexible, which, combined with
  automatic parameter optimization, can improve the compliance/quality tradeoff.
  Non-constant ablation weights were previously explored by Maxime Labonne in
  [gemma-3-12b-it-abliterated-v2](https://huggingface.co/mlabonne/gemma-3-12b-it-abliterated-v2).
* The residual direction index is a float rather than an integer. For non-integral
  values, the two nearest residual direction vectors are linearly interpolated.
  This unlocks a vast space of additional directions beyond the ones identified
  by the difference-of-means computation, and often enables the optimization
  process to find a better direction than that belonging to any individual layer.
* Ablation parameters are chosen separately for each component. I have found that
  MLP interventions tend to be more damaging to the model than attention interventions,
  so using different ablation weights can squeeze out some extra performance.


## Prior art

I'm aware of the following publicly available implementations of abliteration
techniques:

* [AutoAbliteration](https://huggingface.co/posts/mlabonne/714992455492422)
* [abliterator.py](https://github.com/FailSpy/abliterator)
* [wassname's Abliterator](https://github.com/wassname/abliterator)
* [ErisForge](https://github.com/Tsadoq/ErisForge)
* [Removing refusals with HF Transformers](https://github.com/Sumandora/remove-refusals-with-transformers)
* [deccp](https://github.com/AUGMXNT/deccp)

Note that Heretic was written from scratch, and does not reuse code from
any of those projects.


## Acknowledgments

The development of Heretic was informed by:

* [The original abliteration paper (Arditi et al. 2024)](https://arxiv.org/abs/2406.11717)
* [Maxime Labonne's article on abliteration](https://huggingface.co/blog/mlabonne/abliteration),
  as well as some details from the model cards of his own abliterated models (see above)
* Jim Lai's articles describing ["projected abliteration"](https://huggingface.co/blog/grimjim/projected-abliteration)
  and ["norm-preserving biprojected abliteration"](https://huggingface.co/blog/grimjim/norm-preserving-biprojected-abliteration)


## Citation

If you use Heretic for your research, please cite it using the following BibTeX entry:

```bibtex
@misc{heretic,
  author = {Weidmann, Philipp Emanuel},
  title = {Heretic: Fully automatic censorship removal for language models},
  year = {2025},
  publisher = {GitHub},
  journal = {GitHub repository},
  howpublished = {\url{https://github.com/p-e-w/heretic}}
}
```


## License

Copyright &copy; 2025-2026  Philipp Emanuel Weidmann (<pew@worldwidemann.com>) + contributors

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

**By contributing to this project, you agree to release your
contributions under the same license.**
