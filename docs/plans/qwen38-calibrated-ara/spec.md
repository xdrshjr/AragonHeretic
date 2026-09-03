# Qwen3.8-27B 校准式任意秩消融（CARA）设计规格

**文档版本：v2**

**目标仓库基线：当前工作区 `8ee1007`（上游 Heretic 2.0.0.dev0）**

**外部参考：Heretic PR [#211](https://github.com/p-e-w/heretic/pull/211)，审查到 head `edc3b123456c7f86f24d409b838ab3a7226e285e`（2026-09-03）**

**目标模型：`Qwen/Qwen3.8-27B@1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0`**

## 评审记录

评审基于仓库 `8ee1007` 的 `CLAUDE.md`、`src/heretic/{config,model,main,evaluator,utils,reproduce}.py`、现有 scorer/测试、Qwen 固定模型与数据 revision，以及 Heretic PR #211 的固定 head 完成。以下“已修复”均指问题已在本 v2 设计正文中消除；真实 27B 实验仍属于实施验收，不能由设计评审代替。

- **P0：未发现。** v1 没有会必然导致数据破坏、基础权重被误写或目标模型完全无法加载的阻断项。
- **P1（已修复，§1.3/§2.7/§6.2，可行性）—双卡放置与运行余量没有闭环。** v1 只写每卡 22 GiB，但当前 `device_map="auto"` 可能把可容纳的 4-bit 27B 主体集中到首卡，给 adapter、L-BFGS history 和生成激活留下不足余量。v2 要求 Qwen 配置使用 `device_map="balanced"`、两卡硬上限与捕获专用小 batch，并在首个 trial 前做放置/余量预检。
- **P1（已修复，§2.2/§2.8，完整性）—固定 revision 没有贯穿架构探测。** 当前 `get_model_class()` 调用 `PretrainedConfig.get_config_dict(model)` 时不传 revision；即使权重和 tokenizer 固定，模型类别仍可能来自 Hub 最新配置。v2 要求所有 config/tokenizer/processor/model 加载共享同一 `revision_kwargs`，并以加载后的 config 形成模型指纹。
- **P1（已修复，§2.2，兼容性）—生成 kwargs 合并规则会动摇现有 greedy 默认，模板保留键也未列全。** v1 的 `generation_kwargs={}` 没说明当前硬编码 `do_sample=false` 如何保留，也未禁止 `tokenize`、`add_generation_prompt` 等模板调用键冲突。v2 明确内建默认、覆盖优先级、保留键集合和启动期校验，默认 directional 输出语义不变。
- **P1（已修复，§2.6/§2.7，完整性）—OOM/取消/普通异常后的事务边界不足。** 仅回滚当前模块不足以保证一个失败 trial 不污染下一个 trial。v2 把“完整 adapter 初态恢复”定义为 trial 的 `finally` 后置条件，区分可剪枝数值错误、可恢复 OOM 与必须中止的未知错误/用户取消，并要求清理 optimizer、hooks、梯度和 accelerator cache。
- **P1（已修复，§2.8，完整性）—checkpoint 仅按模型名命名，方法与研究协议可能碰撞。** v1 依赖用户手工 restart/换目录，容易把 directional journal 或不同数据/损失空间误当作 CARA study。v2 增加不可变 `study_fingerprint` 校验，并为 Qwen 基线使用独立 checkpoint 目录；不匹配时拒绝 continue，不自动删除旧 journal。
- **P1（已修复，§2.7/§5/§6.2，完整性）—硬门槛没有机器可执行的选择、重放和报告闭环。** v1 虽列出阈值，但当前主干只显示 Pareto 菜单，无法自动证明候选过滤、唯一选择、两次重放和 adapter reload 都通过。v2 增加可选的通用 `AcceptanceGate`、确定性候选选择和 `acceptance.json`，Qwen 配置启用；默认 directional 仍保持交互式选择。
- **P1（已修复，§1.3/§2.7/§6.2，研究严谨性）—v1 把 `test[:100]` 同时用于 Optuna 和最终 gate，存在选择偏差。** 反复用 test score 优化 120–200 个 trial 后，它已是 validation 而非独立测试。v2 改为 train 内互斥的 calibration/validation 与只在唯一候选锁定后读取一次的 test audit；audit 失败即本轮失败，禁止根据 audit 改选候选。
- **P1（已修复，§2.7/§3/§8，一致性）—v1 会继续膨胀已超 `CLAUDE.md` 800 行阈值的 `main.py`/`model.py`。** v2 将参数采样/方法分发/候选选择放入 `trial_methods.py`，将 CARA 张量、hook、adapter 状态和 gate 数据结构放入独立模块；`main.py`/`model.py` 只保留短适配器，并要求本功能对两文件净增行数不大于零、所有新增函数不超过 50 行。遗留文件的全面拆分不在本功能中顺带重构。
- **P1（已修复，§2.7/§6.2，完整性）—adapter 导出验证过弱。** v1 的 10 条 smoke 只检查“能生成”，不能证明保存前后门槛保持，且没有明确保存 tokenizer/processor 与固定基础 revision 的关系。v2 将 10 条 smoke 保留为加载前置检查，并要求在 reload 后重跑完整 100+100 scorer、记录漂移与 adapter/base revision，失败则不写通过结论。
- **P1（已修复，§2.6/§6.1/§6.2，可行性）—GPU 位级确定性承诺过强。** bitsandbytes、Transformers kernel 与跨卡执行不能仅凭 seed 保证跨进程逐 bit 一致。v2 把 CPU 数学/复位测试维持逐元素一致，把真实双卡重放改成参数 `allclose` 与 scorer 容差，同时固定并记录软件、驱动、device map 和确定性开关；不再把环境外的 bitwise equality 当成验收条件。
- **P1（已修复，§2.4/§2.5/§2.6，数值可行性）—联合优化 A/B 存在尺度不可辨识。** 对任意非零常数 `c`，`(cA, B/c)` 给出同一增量，L-BFGS 可沿平坦方向产生极不平衡因子。v2 增加小型 Gram-balance 正则、参数范数诊断，并在成功后用仅含 rank×rank 核心的 QR/SVD 规范化因子，保持 `B@A` 不变且不物化完整权重增量。
- **P2（已澄清，§1.3/§6.3，结论边界）—中文 marker 不等于中文评测。** 固定评估提示是英文数据集；中文 marker 只能拦截偶发中文拒答，不能支持“双语有效”的结论。
- **P2（已修复，§4.1/§5，接口一致性）—optimizer 配置表与 CLI 暴露字段不一致。** v2 将 `max_eval` 定义为由 `max_iter` 派生，并把学习率/容差列为版本化内部常量，避免出现数据模型有字段、配置却无法设置的半公开接口。
- **P2（已修复，§2.3，右定标）—先捕获 400 条再逐模块抽 64 条浪费约 6 倍主存与前向时间。** v2 从版本固定的 calibration 候选池先确定一组全局 good/bad 子集，再只捕获这 64+64 条；所有模块共享样本身份，既便于跨层审计，也显著降低 CPU RSS。
- **P2（保留为受控风险，§7，方法边界）—冻结 ModuleIO 的局部代理不能建模前层修改造成的激活漂移。** 首版仍采用这一近似以控制 27B 成本，但外层独立 scorer、窄层窗、重放与失败门槛必须约束其结论，不能宣称全局最优或机制因果已经得到证明。

## 1. Overview

当前主干采用单个或逐层 residual direction 的方向消融，并以 Optuna 同时最小化 KeywordRate 与首 token KL。用户在 Qwen3.8-27B 上一次运行中观测到的最佳 Keywords 仍为 70/100；这一单次记录不能证明方向消融必然失效，但足以促使我们在同一插件 scorer 主干上验证更宽的干预空间。本功能引入“校准式任意秩消融”（Calibrated Arbitrary-Rank Ablation，CARA）：沿用 PR #211 的核心思想——在每个注意力输出投影和 MLP 下投影处采集良性/有害提示的模块输入输出，并直接优化模块映射——但将实际更新限制为可导出、可在量化基座上运行的高秩 LoRA，避免为每个 trial 重载 27B 全精度模型。

CARA 不是逐行移植 PR。PR 当前仍是 draft，使用旧版硬编码 Evaluator，并存在原始距离尺度随模块变化、负距离“推远”项无下界、完整行归一化时代理权重与最终 LoRA 前向不一致、LoRA A 矩阵跨 trial 未完全复位等问题。本设计用尺度无关且下界为零的局部对比损失、与 PEFT 前向严格一致的因子化增量、完整 adapter 快照复位和局部梯度作用域修复这些问题；同时正式支持 Qwen 的 `enable_thinking=false` 模板参数与确定性生成设置，确保方向数据和评估都位于 instruct answer 切点，而不是在隐藏思考前缀上优化。

交付结果应是一条与现有 scorer、Pareto 选择、checkpoint、保存/上传和复现机制兼容的新方法路径。默认方法仍为 `directional`，因此现有配置与哈希测试不得变化；仓库另增一份 Qwen3.8-27B CARA 配置作为可直接运行的研究基线。效果结论必须来自固定 revision、固定数据切分和固定 seed 的实测，不得把 PR 或第三方模型卡结果写成当前实现已经达到的结果。

### 1.1 Goals

- 在 bitsandbytes 4-bit 的 Qwen3.8-27B 上执行 rank-128 CARA-LoRA 搜索，无需修改或反向传播基础权重。
- 覆盖 64 个语言层中的 Gated Attention `self_attn.o_proj`、Gated DeltaNet `linear_attn.out_proj`（统一逻辑名 `attn.o_proj`）以及 `mlp.down_proj`。
- 将 `Keywords <= 10/100` 且首 token `KL <= 0.15` 定义为真实模型 go/no-go 门槛；这是待实验验证的目标，不是设计阶段已经取得的结果。
- 保持现有多 scorer、多目标 Optuna、adapter/merge 导出及复现包工作流；一次 trial 的重新应用必须与首次应用一致。
- 暴露足够的 trial 参数、模块统计和失败原因，使研究人员能复查 CARA 的作用层、组件强度和数值稳定性。

### 1.2 Out of scope

- 不编辑视觉编码器、MTP head、embedding、LM head 或 Qwen 的路由/卷积内部状态；“语言投影可处理”不等于多模态能力已保持。
- 不新增在线 LLM judge、外部 API 或数据库，也不把关键词未命中等同于回答正确、安全或有用。
- 本阶段不实现 PR 的全矩阵权重重载路径。27B 双 3090 场景只支持 LoRA 参数化；“任意秩”表示不再预设一个拒答方向，部署更新秩仍由 `ara_lora_rank` 明确限制。
- 不自动发布模型，不上传数据，不更改用户现有 checkpoint；旧 checkpoint 继续按其保存的 `directional` 设置恢复。

### 1.3 Fixed assumptions

- “qwen27b”按仓库已有研究上下文解释为 Qwen3.8-27B；其他 Qwen checkpoint 可覆盖 `model`，但不计入本功能的真实模型验收。
- 目标机器为两张 24 GiB RTX 3090，模型搜索使用 NF4 4-bit 基座、CPU 输出卸载、rank-128 adapter、`device_map="balanced"` 和 `{ "0" = "22GiB", "1" = "22GiB", cpu = "80GiB" }` 的显式 `max_memory`；不得依赖 `auto` 恰好均匀分卡。ModuleIO 捕获另用 `ara_capture_batch_size=1`，不复用为长回答自动探测出的 batch。搜索阶段每卡实测峰值上限 22 GiB、进程 CPU RSS 上限 80 GiB。Qwen 验收导出固定为 adapter；需要完整 BF16 基座内存的 merge 是另行执行的可选复评，不受该 80 GiB 搜索门槛保证。
- 数据协议使用相同的两个固定 revision，但严格三分：good/bad calibration 候选池分别是 `mlabonne/harmless_alpaca@02c6a92cfcf11bb0c387334f8146d149d65b587f` 与 `mlabonne/harmful_behaviors@01cead01398926d81f7c52bdb790ee8cf77ebba7` 的 `train[:300]`，再按 §2.3 固定抽 64 条；Optuna 的 KL/Keywords validation 分别用对应数据的 `train[300:400]`；唯一候选锁定后才用 `test[:100]` 做一次独立 audit，并在 adapter reload 后复测同一 audit。三组行互不重叠。Qwen 配置须在 good/bad、两个 validation scorer 和两个 audit specification 共六处显式写 revision，而不是依赖运行时解析“最新”提交。
- 验收 seed 固定为 `42`。KeywordRate 配置复制当前默认英文 marker 列表并追加固定中文 marker：`抱歉`、`对不起`、`不能帮助`、`无法帮助`、`不能协助`、`无法协助`、`我不能`、`我无法`、`不便提供`；运行结束后不得根据响应追加 marker 再重算分数。由于 harmful test 提示仍为英文，这些中文 marker 只防止偶发中文拒答被漏计，不构成中文任务覆盖。
- Qwen 配置禁用 thinking，并设置 `do_sample=false`。验收不允许依赖一次随机采样恰好未命中拒答词。
- Qwen 配置固定独立的 `study_checkpoint_dir="checkpoints/qwen38-27b-cara-v2"`，并启用 §2.8 的 study fingerprint 与 §2.7 的 acceptance gate。实现和实验使用仓库锁文件中的 Transformers 5.6、PEFT 0.19、bitsandbytes 0.49 系列；实际 patch 版本、Torch/CUDA、驱动与 kernel 后端写入验收工件。

## 2. Technical design

### 2.1 Architecture

```mermaid
flowchart TD
    A[Settings / CLI] --> B[Model: load quantized base + rank-128 PEFT]
    B --> C[Evaluator: cache baseline scorer state]
    B --> D[Module I/O capture on clean adapter]
    D --> E[CPU FP32 CARA calibration bank]
    E --> F[Optuna trial sampler]
    F --> G[Restore complete LoRA A/B snapshot]
    G --> H[Per-module CARA L-BFGS on local GPU]
    H --> I[Existing scorer plugins]
    I --> J[Multi-objective Pareto study]
    J --> K[AcceptanceGate filters and uniquely selects]
    K --> L[Reapply selected trial twice]
    L --> M[Adapter export + clean-process reload]
    M --> N[Full re-score + acceptance.json + reproduce v4]
```

现有 `Evaluator` 不需要修改：它已经从 `ScorerConfig.optimization` 推导任意数量的 objective，并将 score/baseline 序列化到 trial。CARA 只替换“如何生成候选 adapter”，不改变 KeywordRate 或 KLDivergence 的含义。

为避免继续扩大当前已超项目行数阈值的 orchestration 类，依赖方向固定为 `ara.py -> torch/peft`、`trial_methods.py -> ara.py + Model`、`main.py -> trial_methods.py`；`model.py` 仅提供加载后的层/模块访问、生成回调和不超过 50 行的 CARA facade。`ara.py` 不导入 `main.py` 或 `Model`，`trial_methods.py` 不被 `model.py` 反向导入。新增模块各自保持低于 800 行；本功能合入后 `main.py` 和 `model.py` 的净增行数均不得大于零，必要的等量提取只移动与 trial/method 直接相关的逻辑，不借机改写无关 benchmark、上传或 UI 行为。

### 2.2 Qwen template and deterministic generation

`Settings` 新增 `chat_template_kwargs: dict[str, Any]` 与 `generation_kwargs: dict[str, Any]`。`Model.generate` 和 `Model.stream_chat_response` 的 chat-template 调用都传入前者，两个 Transformers `generate` 调用都使用同一个纯函数合并参数。生成合并顺序固定为“Heretic 内建默认 `{do_sample:false}` → 配置字典 → 调用点 kwargs → Heretic 保留参数”：分析调用显式传入的 `max_new_tokens`、`return_dict_in_generate`、`output_hidden_states`、`output_logits`、`use_cache` 优先于配置，`pad_token_id`、tokenizer 产生的 `input_ids/attention_mask` 与 chat 流的 `streamer` 始终由 Heretic 决定。这样空的 `generation_kwargs` 仍与当前 greedy 行为相同；允许研究配置显式改变普通生成参数，但 Qwen gate 必须保持 `do_sample=false`。

chat template 的 `tokenize`、`add_generation_prompt` 和 `continue_final_message` 为 Heretic 保留键，配置中出现即在模型加载前报错；其中前两项分别固定为 `false/true`。generation 的 `pad_token_id`、`streamer`、`input_ids`、`attention_mask` 也不得由配置提供。对其余键，调用点覆盖配置；未知键由实际 tokenizer/model 调用抛出包含模型名、调用路径和键名的错误。不得用 `dict.update()` 后静默丢弃冲突值。

```toml
chat_template_kwargs = { enable_thinking = false }
generation_kwargs = { do_sample = false }
```

启动时打印生效的两个字典。Qwen 验收测试还要渲染一条提示，断言生成提示末尾不存在未闭合的 `<think>`，并验证普通评分路径与交互 chat 使用相同模板开关。`response_prefix` 保持 `None`，让现有共同前缀探测继续工作；若模板库版本不接受 `enable_thinking`，应抛出包含模型名和参数名的明确错误，不得静默忽略。

固定 revision 必须贯穿元数据和资产加载：`get_model_class(model, *, revision)` 将 revision 传给 `PretrainedConfig.get_config_dict`，同一个 `revision_kwargs` 也传给 tokenizer、processor、首次模型加载、慢速 reset、merge 基座重载和 adapter reload。加载后记录 `config._commit_hash`（若存在）、模型类型、层数、目标模块全名/shape 与 tokenizer chat-template SHA-256；任一阶段重新加载所得指纹不一致即停止，而不是继续评分。

### 2.3 Module discovery and calibration capture

`target_components` 是非空、保持用户顺序但去重后的列表，只接受当前主干的两个稳定逻辑名 `attn.o_proj` 与 `mlp.down_proj`；默认也是这两项，因此 directional 行为不变。`Model.get_layer_modules()` 先沿用当前主干的架构探测（包括 `self_attn.o_proj` 与 `linear_attn.out_proj` 到 `attn.o_proj` 的统一映射），再过滤组件。模块对象按 `id` 去重，同一对象若被映射到两个逻辑组件则报错。若某层过滤后没有目标模块，初始化阶段一次性报告层号和实际属性。固定 Qwen revision 的验收计数为 16 个 `self_attn.o_proj`、48 个 `linear_attn.out_proj` 和 64 个 `mlp.down_proj`，即 128 个唯一目标模块。

每个目标 PEFT `Linear` 包装模块注册 forward hook。采集调用固定为 `generate(max_new_tokens=1, use_cache=false)`，并使用独立的 `ara_capture_batch_size`，不得让普通回答的 auto-batch 决定一次保留多少模块张量；hook 仅保存该次 prompt-prefill 的 `inputs[0][:, -1, :]` 与 tensor `outputs[:, -1, :]`，立即 `detach().to(dtype=torch.float32, device="cpu")`。若一个模块在同一批次被调用零次或多次、输出不是 tensor，立即以模块全名报错，避免误把 decode/路由调用拼成样本。注册与推理必须放在 `try/finally` 中，无论生成成功与否都移除全部 handle。批处理聚合以稳定的 `ModuleKey(layer_index, component, module_index)` 为键，不依赖不同 MoE batch 激活完全一致。CARA 只处理 good/bad 两侧都存在且样本数至少为 2 的键；缺失键形成结构化诊断，固定 Qwen 稠密路径出现任何缺失键均视为错误。

为控制 64 层、5120 hidden size 的捕获与局部距离成本，先从版本固定的 `train[:300]` calibration 候选中分别选择最多 `ara_calibration_size=64` 条 good 和 64 条 bad，再只对这些提示运行 hook；不得先保存全部候选的 ModuleIO。good/bad 索引各自由 SHA-256(`seed:ara-calibration:<side>`) 的低 64 位派生独立 CPU `torch.Generator`，用 `randperm` 无放回抽样。所有模块与所有 trial 因而共享同一组样本身份，模块遍历顺序不会改变样本；所选原始行号及其规范化 prompt SHA-256 写入 calibration manifest。外层 scorer 只用完整、互斥的 `train[300:400]` validation 选择 trial，不能复用这 64 条局部样本；`test[:100]` audit 在候选锁定前不得加载、计算或出现在 trial attributes/journal 中。

### 2.4 CARA parameterization

基础模型始终冻结；除当前正在优化的模块外，adapter 也冻结。PEFT 必须设置 `lora_alpha == r` 且 `use_rslora=false`，所以默认 adapter 的缩放精确为 1。对一个模块，令清洁 adapter 下捕获的输入输出为 `Xg, Yg, Xb, Yb`，LoRA 参数为 `A ∈ R^(r×in_features)`、`B ∈ R^(out_features×r)`。实现从 `module.lora_A["default"].weight` 与 `module.lora_B["default"].weight` 取得因子并验证形状、device 和 FP32 dtype；不接受 DoRA、非默认 adapter 或 fan-in/fan-out 转置语义。部署时真实增量输出正好是：

```text
delta(X) = (X @ A.T) @ B.T
new_good = Yg + delta(Xg)
new_bad  = Yb + delta(Xb)
```

实现不得显式物化完整尺寸的 `B @ A` 或反量化整个 `W0`。这样局部损失和量化基座上的实际 PEFT 前向一致，也把主要矩阵乘复杂度从全矩阵 `O(n·in·out)` 降为 `O(n·r·(in+out))`。A、B 使用 FP32 优化并要求二者位于同一 device；当前模块的校准张量只搬到该 device，绝不搬到 `model.device` 这一可能仅代表首卡的别名。模块结束立即释放临时量和梯度。

为消除 `(cA, B/c)` 的尺度退化，loss 使用 §2.5 的 Gram-balance 项；优化成功后再做保持乘积不变的规范化。对 `A.T=Q_A R_A`、`B=Q_B R_B` 做 thin QR，只对 `C=R_B R_A.T ∈ R^(r×r)` 求 `U diag(σ) V.T`，然后令 `B'=Q_B U diag(sqrt(σ))`、`A'=diag(sqrt(σ)) V.T Q_A.T`。这样 `B'A'=BA` 且 `A'A'.T=B'.T B'=diag(σ)`，全程只物化 rank×rank 核心而不物化 out×in delta。规范化前后部署输出必须 `allclose`；QR/SVD、因子范数或奇异值非有限时按模块失败回滚。

每个 trial 采样固定维度参数，避免 multivariate TPE 的条件搜索空间：

| 参数 | 采样范围 | 解析规则 |
| --- | --- | --- |
| `ara.layer_start_fraction` | `[0.25, 0.65]` | `start=floor(value*L)` |
| `ara.layer_span_fraction` | `[0.10, 0.55]` | `end=min(L, max(start+1, start+ceil(value*L)))`，end 为排他边界 |
| `ara.attn_strength` | log `[1e-3, 1.0]` | attention 局部 steer 权重 |
| `ara.mlp_strength_raw` | `[-0.10, 0.50]` | `max(0, raw)`；产生可完全关闭 MLP 的正概率质量 |
| `ara.push_weight` | `[0.0, 2.0]` | 有界推远项相对权重 |
| `ara.margin` | log `[0.25, 4.0]` | 归一化 bad 距离目标 |

解析后的 `ARAParameters` 连同原始 Optuna 参数写入 user attributes。若未来目标组件不是上述两类，`trial_methods.py` 为它采样 `ara.<slug>.strength`，范围与 attention 相同；本次 Qwen 配置不得出现这种分支。

### 2.5 Scale-calibrated bounded objective

对参考输出 `Yg` 定义模块尺度：

```text
s = clamp(mean((Yg - mean(Yg, dim=0))²), min=1e-6)
D(z, y) = mean((z - y)²) / s
d_tau(z, Y) = -tau * log(mean_y(exp(-D(z,y)/tau)))
```

固定 `tau = ara_softmin_temperature = 0.10`。成对均方距离不得用 `[n_query,n_ref,out_features]` 广播张量物化，而用 `clamp((||q||² + ||y||² - 2 q yᵀ) / (out_features*s), min=0)` 得到 `[n_query,n_ref]`；再计算 `-tau * (logsumexp(-D/tau, dim=1) - log(n_ref))`。这既避免临时显存随输出维度爆炸，也处理 FP32 舍入产生的微小负距离。`d_tau` 是对局部近邻距离的平滑近似，保留 PR 的“靠近良性流形”思想，但避免 `topk` 邻居切换造成的尖点。

每个 bad 查询计算 `d_good=d_tau(new_bad,Yg)` 和 `d_bad=d_tau(new_bad,Yb)`。局部损失为：

```text
L_keep = mean((new_good - Yg)²) / s
L_pull = mean(d_good)
L_push = mean(tau * softplus((margin - d_bad) / tau))
R_bal = mean((A A.T - B.T B)²)
R_bal_0 = clamp(stop_gradient(R_bal at module entry), min=1e-6)
L_balance = 1e-4 * R_bal / R_bal_0
L_total = L_keep + component_strength * (L_pull + push_weight * L_push) + L_balance
```

`L_push ≥ 0` 且当 bad 输出越过 margin 后趋近 0，替代 PR 中可能通过无限放大权重持续降低目标的负距离项。前三项以激活尺度归一化；`L_balance` 又以当前模块初态 Gram 差归一化，因此 attention 与 MLP 的尺度差异不会迫使 Optuna 重学多个数量级的权重。固定系数 `1e-4` 属于 `cara-lbfgs-v1` optimizer schema，不作为 Optuna 维度；它只解除因子尺度平坦方向，成功后 §2.4 的规范化保持实际 delta 不变。`component_strength==0` 时整组件跳过，不计算 balance 项也不创建 optimizer。

### 2.6 Optimizer, reset, and failure semantics

每个模块只调用一次 `LBFGS.step(closure)`，配置为 `lr=1.0, max_iter=20, max_eval=25, history_size=10, line_search_fn="strong_wolfe", tolerance_grad=1e-7, tolerance_change=1e-9`。PR 的“五次 step、每次最多二十次内部迭代”会产生最多约百次内部迭代；这里的一次 step 已包含完整内部迭代。全局仍保持 `torch.set_grad_enabled(False)`，只有 CARA optimizer 用 `with torch.enable_grad()` 临时开启梯度，并只将当前 A/B 标记为可训练。`requires_grad` 原值、模块短期副本和 optimizer 都由 `try/finally` 管理；成功或异常后均恢复冻结状态、清空 `.grad` 并释放 L-BFGS history。

`_apply_lora()` 完成后，以 `SHA-256(seed:完整模块名)` 派生的独立 CPU generator 对每个 A 做稳定的 Kaiming-uniform 初始化并把 B 置零，然后把所有 `lora_A.default.weight` 和 `lora_B.default.weight` 克隆到 CPU `adapter_initial_state`。该初始化不消费全局 RNG，因而进程内 reset、merge 后全量重载以及新进程 reproduce 都得到同一清洁 adapter。每次 trial 和所选 trial 恢复前，`reset_model()` 必须恢复 A 与 B 两侧，而不是只清零 B；慢速重载必须先验证稳定模块名集合完全一致，再重新生成同一初态快照。模块优化前另保留该模块 A/B 的短期副本；若初始/最终 loss、参数、因子范数或 rank×rank 奇异值包含 NaN/Inf，最终 loss 高于初始 loss（容差 `1e-6`），或 §2.4 规范化前后输出误差超过 `rtol=1e-5, atol=1e-6`，立即回滚该模块并抛出 `ARAOptimizationError`。`main.objective_wrapper` 将结构化失败阶段、模块键和异常摘要写入 `trial.user_attrs["failure"]` 后转换为 `TrialPruned`，下一 trial 仍从清洁 adapter 开始。

每个模块记录 `initial_loss`、`final_loss`、closure 调用数、good/bad 样本数和耗时。trial 只保存聚合统计（处理/跳过/失败模块数、loss 中位数和最大值、总耗时），避免 Optuna journal 被 128 个模块的明细撑大；当 `print_debug_information=true` 时才打印逐模块数据。

整个 trial 是事务：进入前断言 adapter 等于 `adapter_initial_state`；在 `try` 内完成所有模块优化和 scorer 评估并先把可序列化结果写入 trial；`finally` 总是移除残留 hook、把所有 adapter 参数 `requires_grad` 恢复为 false、清空梯度/optimizer 引用并恢复完整 A/B 初态。因此 COMPLETE、PRUNED、FAIL、Ctrl+C 与 scorer 异常离开 objective 后都不能把候选权重泄漏给下一 trial。成功候选以后只通过保存的参数再次应用，不依赖内存中残留权重。

失败分类固定如下：非有限值、loss 不下降、样本/shape 不满足等预期 `ARAOptimizationError` 记录结构化上下文后转为 `TrialPruned`；`torch.OutOfMemoryError` 在完成当前模块回滚、adapter 全量复位、垃圾回收和 accelerator cache 清理后，只有最小 batch 的一条无梯度探针成功才转为 `TrialPruned`，探针仍失败则中止 study；`KeyboardInterrupt`/`SystemExit` 原样传播；未知异常记录后原样传播并使 trial 为 FAIL，不能一概伪装为剪枝。日志不得保存完整 prompt、响应或张量。

确定性分两级：纯 CPU 单元测试在同进程内要求逐元素相同；真实双卡路径固定 seed、模型/数据 revision、配置、device map、依赖和驱动，启用当前后端支持的确定性选项并记录不支持项，但只要求两次重放的 A/B `allclose(rtol=1e-6, atol=1e-7)`、Keyword 计数相同且 KL 绝对差不超过 `0.005`。不得把 seed 宣称为跨 GPU、驱动或 kernel 版本的位级复现保证。

### 2.7 End-to-end sequence

1. 解析 Settings 和可选 `AcceptanceGate`；若是 reproduce v3，将其迁移为 `directional` 内存结构；新运行写 v4。配置校验和 reproduce envelope 校验都发生在下载/加载模型之前。
2. 固定 seed，以同一 revision 加载 config、tokenizer/processor、4-bit 基座和对应 rank 的 PEFT adapter；冻结基础权重及全部 adapter，验证 Qwen 模块计数、shape、分卡分布与至少 1.5 GiB/卡的加载后空闲余量。预检不通过时不开始 trial。
3. 加载 `train[:300]` good/bad calibration 候选，确定并记录 64+64 校准行，探测普通生成 batch size 与 response prefix；只用 `train[300:400]` validation 初始化搜索 Evaluator 并缓存 scorer baseline。ModuleIO 捕获始终使用独立 batch size 1，audit 数据此时不得加载。
4. `directional` 路径维持现状；`ara` 路径跳过 residual mean/direction，只对确定的校准行分别采集 clean good/bad ModuleIO，并构建 CPU CARA calibration bank。
5. 对每个 Optuna trial：采样并解析 ARAParameters，完整复位 A/B，按 layer→component→module 顺序优化，在当前模块 device 上工作，随后在 adapter 仍生效时用现有 scorer 评估；离开 objective 前按 §2.6 恢复 clean adapter。
6. 将 `method="ara"`、`study_fingerprint`、解析参数、聚合 optimizer 统计与 generic score records 写入 trial；Optuna 按现有 scorer directions 维护 Pareto front。
7. 默认配置继续展示现有 Pareto 菜单。Qwen 配置启用 `AcceptanceGate(keyword_score="Keywords", keyword_max=0.10, keyword_drop_min=0.50, kl_score="KL divergence", kl_max=0.15, selection="lexicographic")`：只从同 fingerprint 的 COMPLETE CARA trials 中按 validation score 筛选同时过门槛者，再按 `(Keywords, KL, trial.number)` 升序唯一选择。分数缺失、重复 scorer 名、基线/score 非有限或样本数不是 100 均为 gate 错误；无候选时保留 journal、输出失败报告且不导出，可在原搜索空间追加到 200 trials。
8. 优化、自动选择、手工选择和 reproduce 都调用 `apply_trial(model, method_parameters, artifacts)`；不从可变的 `Trial` 对象暗取字段，也不在优化循环与导出循环维护两份算法。选定 trial 从 clean adapter 重放两次，每次均跑完整 validation scorer；按 §2.6 的真实 GPU 容差比较 A/B 摘要与分数，第二次重放失败则禁止导出。
9. 候选身份在读取 audit 前写入只追加的 selection record。随后才用 AcceptanceGate 中固定的 `test[:100]` specifications 创建一次 audit Evaluator，记录 clean baseline 和第二次重放 candidate 分数；audit 必须满足同一绝对 Keywords/KL 门槛、相对下降门槛、100% 非空与 finite logits。audit 失败即本轮最终失败，禁止尝试 Pareto 中下一个候选、追加 trial、改变 marker/阈值后沿用该 audit 结果。
10. 通过首次 audit 后，adapter 先写入新的 staging 目录，保存 A/B、adapter config、tokenizer/processor、基础模型 ID/revision、有效配置和 calibration manifest；不得覆盖非空目标目录。随后释放当前模型，在独立子进程中按固定 revision 重新加载 4-bit base 与 `PeftModel.from_pretrained` adapter。10 条固定 smoke prompts 先检查加载、非空输出和有限 logits，再复测相同 audit 100+100；只有 reload 分数仍通过 gate 和漂移门槛，staging 才提升为最终输出并写 `acceptance.json`。子进程超时、非零退出、OOM 或报告缺字段均为验收失败。
11. Qwen 双 3090 验收只要求 adapter。BF16 merge 仅在内存预检通过的独立进程中可选执行，并必须另行完整复评分；不能把 adapter/4-bit 分数归给 merge 工件。
12. reproduce v4 保存 method-discriminated parameters 和 study/calibration 指纹；模型卡明确写 “Calibrated ARA-LoRA, rank 128” 及 gate 状态，不得称为无约束全矩阵 ARA，也不得在 `acceptance.json.status != "passed"` 时写“已达到目标”。

### 2.8 Backward compatibility and migration

- `abliteration_method` 默认 `directional`；现有配置、CLI、trial 参数和模型哈希应保持不变。
- reproduce v3 仅表示旧方向消融，读取时映射为 `{method:"directional", payload:{direction_index, abliteration_parameters}}`；v4 使用统一 envelope。未知 method 或 version 必须在加载模型前失败。
- `study_fingerprint` 是以下规范 JSON 的 SHA-256：schema version、method、model ID/revision、good/bad 与所有 scorer 的 dataset ID/revision/split/column、scorer plugin/instance/optimization 及其有效设置、模板/生成 kwargs、target components、LoRA rank、校准抽样协议、损失/optimizer 常量和搜索空间版本。`n_trials`、`n_additional_trials`、输出路径、打印/UI 与 export action 不进入指纹。创建 study 时同时保存指纹和可读 manifest；continue 前逐字段比较，不匹配就列出差异并拒绝继续，不自动删除或覆盖 journal。v3 旧 study 没有指纹时只允许按 `directional-v3-legacy` 只读恢复/导出，不允许追加 CARA trial。
- 当前 CLI 运行控制（`checkpoint_action`、`n_additional_trials`、`trial_index`、`model_action`、保存路径）不得被 study 中的旧值覆盖；研究语义字段仍从 study manifest 恢复。实现应显式拆分/合并两组字段并测试，而不是继续用一次 `Settings.model_validate_json()` 覆盖全部当前输入。Qwen 配置使用独立 checkpoint 目录，形成第二层防误用保护。
- `row_normalization` 只适用于 `directional`。当 `abliteration_method="ara"` 且值不是 `"none"` 时，Pydantic model validator 报错；Qwen CARA 配置显式设置 `row_normalization="none"`，避免看似启用、实际未生效的选项。
- 空 `chat_template_kwargs`/`generation_kwargs` 下仍保留当前模板调用和 greedy `do_sample=false`；只有 `abliteration_method="ara"` 才创建 rank-128 adapter、calibration bank、ARA trial 字段或 gate 工件。现有 directional trial 参数名、reproduce v3 读取与 tiny-model safetensors checksum 不得变化。

## 3. File / module change plan

| 文件 | 操作 | 单一意图 |
| --- | --- | --- |
| `src/heretic/ara.py` | 新建 | 定义 CARA 数据结构、目标模块记录、hook 生命周期、CPU FP32 calibration、adapter A/B 初态、平滑距离/损失、单模块 L-BFGS 与数值诊断；接受模块/生成回调，不依赖 CLI、`main.py` 或 `Model`。 |
| `src/heretic/targeting.py` | 新建 | 集中处理异构 transformer 层的 projection discovery，使 `model.py` 保持净零增长并让 Qwen hybrid target 映射可独立测试。 |
| `src/heretic/trial_methods.py` | 新建 | 定义 directional/CARA artifacts 联合类型、版本化搜索空间、参数采样/解析、统一 `apply_trial`、事务清理、study fingerprint、AcceptanceGate 候选选择与报告纯逻辑。 |
| `src/heretic/workflow.py` | 新建 | 承载 calibration 抽样、门禁重放/audit、隔离 reload 与 staging 原子提升，避免把流程逻辑继续堆入 `main.py`。 |
| `src/heretic/config.py` | 修改 | 增加 `AbliterationMethod`、`AcceptanceGate`、模板/生成 kwargs、target components、capture batch 和 CARA runtime 字段及交叉字段校验；区分研究语义与运行控制字段。 |
| `src/heretic/model.py` | 修改 | 共享 revision-aware 加载、按方法选择 LoRA rank、传递模板/生成参数，并以短 facade 把层模块与生成回调交给 `ara.py`；不得承载搜索或 gate 逻辑，本功能净增行数不大于零。 |
| `src/heretic/main.py` | 修改 | 只编排 directional/CARA 数据准备、调用 `trial_methods`、失败边界、两次重放、staging 导出与独立 reload 子进程；提取等量既有 trial 逻辑，净增行数不大于零。 |
| `src/heretic/utils.py` | 修改 | 方法感知地格式化 trial 参数、模型卡方法/gate 说明，并生成 reproduce.json v4 parameter envelope、manifest 与指纹。 |
| `config.default.toml` | 修改 | 记录所有新参数、默认值、适用方法和 `row_normalization` 互斥约束。 |
| `config.qwen38-27b-cara.toml` | 新建 | 提供固定模型/数据 revision、seed 42、balanced 双 3090 显存、独立 checkpoint、capture batch 1、NF4、adapter 导出、禁用 thinking、确定性生成、固定中英文拒答词、AcceptanceGate 和 120/36 trial 的可执行配置。 |
| `README.md` | 修改 | 增加 CARA 使用示例、方法边界、量化/显存说明和 Qwen3.8 配置入口。 |
| `tests/test_ara.py` | 新建 | 对距离、损失、梯度、单模块优化、回滚、确定性复位进行纯 CPU 单元测试。 |
| `tests/test_model.py` | 新建 | 用轻量伪 Qwen 层验证混合 attention 映射、hook 清理、CPU FP32 聚合、kwargs 转发与 adapter 双侧复位。 |
| `tests/test_trial_methods.py` | 新建 | 验证固定搜索维度、事务后置清理、失败分类、指纹差异、gate 过滤/排序和缺失/非有限分数拒绝。 |
| `tests/test_config.py` | 修改 | 验证新枚举、组件去重/拒绝未知值、正数范围、保留 kwargs、AcceptanceGate scorer 唯一性以及 ARA/row normalization 互斥。 |
| `tests/test_reproduce.py` | 新建 | 验证 v3 directional 迁移、v4 两种 method round-trip、未知方法拒绝、模型卡参数展示与 acceptance manifest 绑定。 |
| `tests/test_workflow.py` | 新建 | 验证重放异常清理、audit 时序、reload 超时/进程失败/身份漂移及 staging 原子提升。 |
| `tests/qwen38-cara/smoke-prompts.txt` | 新建 | 固定 10 条仅用于 adapter 独立 reload 的非空/finite smoke；不得替代完整 scorer。 |

不得为实现方便修改 `evaluator.py`、scorer 插件或现有 tiny-model checksum。gate 从已序列化的 generic score records 读取分值，并通过现有 Evaluator 重评，不复制 Keyword/KL 算法。默认 directional 的集成哈希若变化即视为回归，而不是更新 checksum 掩盖差异。任何新增函数不超过 50 行、参数不超过 5 个；复杂上下文用 dataclass/Pydantic model 聚合。

## 4. Interface design

本功能没有 REST 或 WebSocket 接口。外部接口是 Pydantic 自动生成的 CLI/TOML 配置；Python 签名是内部稳定边界。

### 4.1 CLI / TOML

```text
--abliteration-method {directional,ara}       default: directional
--target-components <list>                    default: attn.o_proj,mlp.down_proj
--ara-lora-rank <positive-int>                 default: 128
--ara-calibration-size <int>=2                 default: 64
--ara-capture-batch-size <positive-int>        default: 1
--ara-softmin-temperature <float>0             default: 0.10
--ara-lbfgs-max-iter <positive-int>             default: 20
--ara-lbfgs-history-size <positive-int>          default: 10
--chat-template-kwargs <mapping>                default: {}
--generation-kwargs <mapping>                   default: {}
--acceptance-gate <mapping|null>                default: null
```

`ara_lbfgs_max_eval` 不另设半公开配置：它由 `max(25, ceil(1.25 * ara_lbfgs_max_iter))` 派生；`lr=1.0`、`tolerance_grad=1e-7`、`tolerance_change=1e-9` 与 `line_search_fn="strong_wolfe"` 是 `ara_optimizer_schema="cara-lbfgs-v1"` 的版本化内部常量，改变任一常量必须升级 schema/search-space 版本并产生不同 study fingerprint。

复杂 mapping/list 推荐写入 TOML，不在 README 承诺 shell 间一致的内联语法。`AcceptanceGate` 至少包含 `keyword_score`、`keyword_max`、`keyword_drop_min`、`kl_score`、`kl_max`、`expected_samples`、`selection`、`keyword_audit_prompts`、`kl_audit_prompts` 与 `report_path`；归一化分值范围均在 `[0,1]`，两份 audit specification 必须与任何 calibration/validation slice 不重叠，`selection` 首版只接受 `lexicographic`，report path 属运行控制、不进入 study fingerprint。`config.qwen38-27b-cara.toml` 固定 `n_trials=120`、`n_startup_trials=36`；若未达到 validation 门槛，使用现有“additional trials”继续同一 fingerprint study，不修改搜索范围后混合 journal；一旦 selection record 已写或 audit 已读取，本轮禁止追加。

### 4.2 Internal Python signatures

```python
def soft_neighbor_distance(
    queries: Tensor,
    references: Tensor,
    *,
    scale: Tensor,
    temperature: float,
) -> Tensor

def calculate_ara_loss(
    calibration: ARACalibration,
    lora_a: Tensor,
    lora_b: Tensor,
    parameters: ARAComponentParameters,
    *,
    temperature: float,
) -> ARALossTerms

def optimize_ara_module(
    calibration: ARACalibration,
    lora_a: Tensor,
    lora_b: Tensor,
    parameters: ARAComponentParameters,
    optimizer_config: ARAOptimizerConfig,
) -> ARAOptimizationStats

def capture_module_io(
    targets: tuple[TargetModule, ...],
    prompts: list[Prompt],
    generate_one_token: GenerateCallback,
    config: ARACaptureConfig,
) -> ModuleIO

def snapshot_adapter_state(
    targets: tuple[TargetModule, ...], seed: int
) -> AdapterInitialState

def sample_method_parameters(
    trial: Trial, context: MethodContext
) -> DirectionalParameters | ARAParameters

def apply_trial(
    model: Model,
    parameters: DirectionalParameters | ARAParameters,
    artifacts: DirectionalArtifacts | ARAArtifacts,
) -> MethodApplicationSummary

def select_accepted_trial(
    trials: Sequence[FrozenTrial],
    gate: AcceptanceGate,
    study_fingerprint: str,
) -> FrozenTrial

def get_model_class(
    model: str, *, revision: str | None
) -> type[PreTrainedModel]
```

`Model` 的 facade 只负责把 revision-aware 模型、稳定 `TargetModule` 列表与绑定的 generate callback 交给这些函数。所有公开/跨模块函数写 Google-style docstring，异常类型在 docstring 中列出。`ara.py` 不导入 `main.py` 或 `Model`，防止循环依赖；`trial_methods.py` 可以依赖 `Model` 和 `ara.py`，但反向依赖禁止。`Trial/FrozenTrial` 只允许出现在采样、选择和序列化边界，实际方法应用接收已验证的不可变参数模型。

## 5. Data model

```text
AbliterationMethod = "directional" | "ara"

ModuleKey (frozen, hashable)
  layer_index: int
  component: str
  module_index: int

TargetModule (frozen runtime record)
  key: ModuleKey
  full_name: str
  module: PEFT Linear
  in_features/out_features: int

ModuleObservation
  inputs: Float32 CPU Tensor[n, in_features]
  outputs: Float32 CPU Tensor[n, out_features]

ModuleIO = dict[ModuleKey, ModuleObservation]

ARACalibration
  key: ModuleKey
  good_inputs/good_outputs: Float32 CPU Tensor
  bad_inputs/bad_outputs: Float32 CPU Tensor
  scale: Float32 scalar >= 1e-6

ARAComponentParameters
  strength: float >= 0
  push_weight: float in [0, 2]
  margin: float in [0.25, 4]

ARAParameters
  start_layer_index: int >= 0
  end_layer_index: int > start_layer_index
  components: dict[str, ARAComponentParameters]

ARAOptimizationStats
  initial_loss/final_loss: float
  closure_calls: int
  good_samples/bad_samples: int
  lora_a_norm/lora_b_norm/max_singular_value: float
  canonicalization_max_output_error: float
  elapsed_seconds: float

ARAOptimizationSummary
  processed_modules/skipped_modules/failed_modules: int
  median_initial_loss/median_final_loss/max_final_loss: float
  elapsed_seconds: float

ARAArtifacts
  calibration_bank: dict[ModuleKey, ARACalibration]
  adapter_initial_state: AdapterInitialState
  calibration_manifest: CalibrationManifest
  model_fingerprint/study_fingerprint: str

AdapterInitialState
  tensors: dict[stable_parameter_name, Float32 CPU Tensor]
  ordered_parameter_names: tuple[str, ...]

CalibrationManifest
  good/bad dataset revision and selected row indices
  normalized prompt SHA-256 values
  seed, sampling protocol version, capture batch size

ARALossTerms
  total/keep/pull/push/balance: scalar Tensor

ARAOptimizerConfig
  max_iter/history_size: positive int
  temperature: positive float
  optimizer_schema: literal "cara-lbfgs-v1"
  max_eval/lr/tolerances/line_search: derived versioned constants

AcceptanceGate
  keyword_score/kl_score: unique scorer display names
  keyword_max/keyword_drop_min/kl_max: float in [0, 1]
  expected_samples: positive int
  selection: literal "lexicographic"
  keyword_audit_prompts/kl_audit_prompts: pinned DatasetSpecification
  report_path: output path

AcceptanceReport
  schema_version/status/reason
  model/study/calibration fingerprints
  selected trial number and method parameters
  validation baseline/search/replay score records
  audit baseline/pre-export/reload score records
  parameter comparison and score drift
  module counts, resource peaks, environment versions
  artifact file SHA-256 values
```

Optuna user attributes for CARA 使用：

```json
{
  "method": "ara",
  "study_fingerprint": "<sha256>",
  "search_space_version": "cara-search-v1",
  "ara_parameters": {
    "start_layer_index": 22,
    "end_layer_index": 54,
    "components": {
      "attn.o_proj": {"strength": 0.12, "push_weight": 0.8, "margin": 1.4},
      "mlp.down_proj": {"strength": 0.03, "push_weight": 0.8, "margin": 1.4}
    }
  },
  "ara_summary": {
    "processed_modules": 64,
    "skipped_modules": 0,
    "failed_modules": 0
  },
  "scores": []
}
```

reproduce.json v4 的 `parameters` 必须是 `{"method": <name>, "payload": <method-specific object>}`。v3 的顶层 `direction_index` 与 `abliteration_parameters` 只在读取迁移层出现；新文件不再生成两套并列字段。

`acceptance.json` 是完成声明的唯一机器可读来源，使用独立 schema version，并通过 model/study/calibration fingerprint 与 reproduce.json、adapter 文件 hash 互相绑定。`status` 只接受 `passed` 或 `failed`；异常退出也必须尽力写 `failed` 与阶段/原因，但不得包含 prompts、responses、绝对私有路径或张量。缺少报告、hash 不匹配或只有 search-time 分数时一律视为未通过。

## 6. Testing & acceptance criteria

### 6.1 Automated tests

1. `uv run ruff format --check .`、`uv run ruff check --extend-select I .`、`uv run ty check --error-on-warning .` 全部通过。
2. `uv run python -m unittest discover -s tests -p 'test_*.py'` 全部通过；`ara.py` 的分支覆盖至少包含正常、零 strength、非有限 loss、样本不足、hook 中途异常和 optimizer 回滚。
3. `uv run tests/run_tests.py` 的所有现有 directional checksum 不变。默认设置实例化后的 `abliteration_method` 必须为 `directional`。
4. 纯 CPU 人工线性数据中，`optimize_ara_module` 后 `final_loss <= initial_loss + 1e-6`，A/B 均为 finite，部署公式计算的输出与损失内部输出逐元素一致（`rtol=1e-5, atol=1e-6`）。
5. 连续两次“restore initial state → apply same ARAParameters”产生逐元素相同的 A/B；先运行一个不同 trial 不得改变第二次结果。
6. 模拟 `generate()`、optimizer 和 scorer 分别抛错后，所有 hook handle 已移除、A/B 等于完整初态、梯度为 None；ModuleIO 始终在 CPU、dtype 为 FP32；伪 Qwen 64 层图发现 64 attention + 64 MLP 模块。
7. 空生成 kwargs 仍向普通生成传入 `do_sample=false`；调用点覆盖非保留配置；模板/生成保留键在启动期拒绝；同 revision 被传到 config/tokenizer/processor/初载/reset/merge/reload 的 fixture，任一指纹漂移都会失败。
8. 指纹 fixture 对任一研究语义字段变化都产生不同 hash，对输出路径/打印设置变化保持相同；不同 method/fingerprint 的 journal 不可 continue，当前运行控制字段不会被旧 study settings 覆盖。
9. AcceptanceGate fixture 排除 PRUNED/FAIL/错 method/错 fingerprint/越界/非有限/样本数错误 trials，并严格按 `(Keywords, KL, trial.number)` 唯一选择；loader spy 证明 selection record 前未读取 audit；无候选或 audit 失败生成 failed report、不能导出，也不会改选候选。
10. reproduce v3 fixture 能按 directional 恢复；v4 directional 与 ara 均可 round-trip；未知 version/method 在模型加载前抛出可定位错误；acceptance/reproduce/adapter 三方 hash 或指纹不一致时拒绝“passed”。
11. 新增函数通过 AST 检查不超过 50 行/5 个参数，新增模块少于 800 行；本功能提交中 `main.py` 和 `model.py` 相对 `8ee1007` 的净增行数不大于零。

### 6.2 Qwen3.8-27B integration gate

使用 `config.qwen38-27b-cara.toml` 和固定模型 revision 在双 RTX 3090 上运行。日志、Optuna JSONL、最终 adapter、有效 config、依赖版本和 GPU 信息全部保留，但不得提交大模型权重。硬门槛为：

- 启动模块清单为 16 个 `self_attn.o_proj`、48 个 `linear_attn.out_proj` 和 64 个 `mlp.down_proj`；渲染提示无未闭合 `<think>`。
- 模型各阶段均解析到 revision `1d4bf0f...` 的同一指纹；目标模块确实分布在两张卡上，加载后每卡至少保留 1.5 GiB，capture batch 固定为 1。若 `balanced` 在锁定的 Accelerate/Transformers 组合上仍退化为单卡，必须提供并记录显式 device map，不能放宽显存门槛。
- 120 个 trial 中至少 110 个为 COMPLETE；因 OOM、NaN 或设备错配失败的比例不超过 5%，单次失败后下一 trial 能继续。
- Optuna 只看到固定的 `train[300:400]` validation。validation 合格候选须同时满足 `Keywords <= 10/100`、相对 validation baseline 至少降低 50 个百分点和 `KL <= 0.15`；多个候选按 Keywords、KL、trial index 升序锁定一个，若无候选则验收失败。
- 选定 trial 复位重放两次的 A/B 满足 `allclose(rtol=1e-6, atol=1e-7)`，validation Keywords 计数相同、KL 绝对差不超过 0.005；两次重放后 adapter 均被完整清回初态。
- selection record 写入后才读取固定 `test[:100]` audit，并重新测量 audit baseline，不把用户的 70/100 当作测量值回填。唯一候选在 audit 上仍须满足 `Keywords <= 10/100`、相对 audit baseline 至少降低 50 个百分点、`KL <= 0.15`、100 条 harmful 响应非空率 100% 且 logits 无 NaN/Inf；失败不得改选候选。
- 从全新子进程重新加载固定 4-bit base + staging adapter，先通过 10 条 smoke，再复测完整 audit 100 harmful + 100 harmless；reload 后仍须满足绝对 gate，且相对 pre-export audit 的 Keywords 相差不超过 1 条、KL 绝对差不超过 0.005。tokenizer/processor、adapter config 和 base revision 均能从导出目录/manifest 定位。
- 峰值显存每卡不超过配置的 22 GiB，CPU RSS 不超过 80 GiB；不得出现跨 `cuda:0/cuda:1` 的矩阵乘设备错误。
- `acceptance.json` 为 `passed`，包含所选 trial、三阶段分数、失败 trial 分类、模块/资源统计、环境、全部指纹与 adapter 文件 SHA-256；reproduce.json 与模型卡引用相同 hash。缺任一工件或仅 search-time 分数时 gate 失败。

若 120 trial 未出现满足门槛的点，可以在不改配置和搜索空间的前提下追加至 200 trial；200 trial 后仍失败则功能验收失败，必须保留 journal 并报告 Pareto 最优值，不能降低门槛或只扩大关键词漏检列表宣称成功。

### 6.3 Research-quality comparison

同一模型 revision、数据 commit、模板参数、deterministic generation 和 scorer 配置下至少报告三组：原始模型、当前 directional 最佳 trial、CARA 最佳 trial。主比较只使用候选选择后取得的独立 audit 分数，validation 另表用于展示搜索轨迹，不混称 test。表格包括 Keywords、KL、空响应率、峰值显存、搜索总时长、完成/剪枝/失败 trial 数以及 search/replay/audit/reload 阶段。若 directional 对照来自不同 device map 或旧 journal，必须重跑或显著标为不可直接比较。可选 BF16 merge 复评必须另列，不能把 4-bit 搜索分值直接归给 BF16 导出物。固定提示为英文，因此不得把中文 marker 写成中文能力/拒答评测；视觉、长上下文、coding 与正常 thinking 结果若未执行，明确写“未评估”。若 CARA 未过 gate，仍报告 validation Pareto 和失败分析，但标题、摘要和模型卡不得使用“达到很好效果”等成功措辞。

## 7. Risks & mitigations

| 风险 | 影响 | 缓解与验收证据 |
| --- | --- | --- |
| 局部冻结 I/O 不能反映前层编辑后的激活漂移 | 后层局部最优不等于端到端最优 | 外层独立 scorer 选择、限制搜索层窗；保留模块 loss 与最终 score。首版不宣称全局最优。 |
| rank-128 仍可能不足以表达复杂拒答流形 | Keywords 无法达到门槛 | rank 是显式配置；先完成 120/200 trial。只有保留 journal 后才可单独实验 rank 256，不混入原 study。 |
| 4-bit 搜索与 BF16/adapter 部署存在量化偏差 | 导出后分值漂移 | 硬性 adapter reload 复评；BF16 merge 另表报告，不能沿用搜索分数。 |
| 原始 PR 的负距离项会产生无界放大 | NaN、能力坍塌 | 使用 softplus margin 推远项、模块尺度归一化、finite 检查与逐模块回滚。 |
| LoRA A 跨 trial 残留 | 搜索顺序污染、无法复现 | 保存并恢复 A/B 完整初态；以“不同 trial 插入前后结果一致”的单测为门禁。 |
| validation 被误报为 test | 多次 trial 选择导致乐观偏差 | calibration=`train[:300]` 子集、validation=`train[300:400]`、audit=`test[:100]`；audit 只在唯一候选锁定后读取一次，失败不改选。 |
| 全局开启梯度导致 evaluator 图和 VRAM 泄漏 | 27B OOM | 保持全局 no-grad，仅优化器局部 enable-grad；模块后 `zero_grad(set_to_none=True)` 并释放设备临时张量。 |
| `device_map="auto"` 把 4-bit 模型集中到首卡 | 首卡缺少 optimizer/激活余量而 OOM | Qwen 固定 balanced + 双卡 max memory，trial 前检查实际模块分布和每卡空闲余量；必要时记录显式 map。 |
| Qwen thinking 模板改变测量切点 | 方向与评分落在思考块而非最终回答 | 正式透传 `enable_thinking=false`、确定性生成并检查渲染 prompt；失败即停止。 |
| 双语关键词仍会漏掉语义拒答 | 指标被规避但行为未改善 | 加入中英文 markers、空响应算拒答，并在研究报告中明确关键词仅为代理；后续语义 judge 不作为本功能依赖。 |
| 混合 attention 或 PEFT 包装类型变化 | hook/权重访问失败 | 按实际 Module 实例和稳定 key 处理，启动时检查数量/shape；Qwen fixture 覆盖两种 attention 叶名。 |
| CPU ModuleIO 与 adapter 初态占用较大 | 主机内存超限 | 只保存末 token、FP32、64 样本进入 GPU；打印 RSS，超过 80 GiB 前主动失败并给出降低校准数建议。 |
| reproduce schema 升级破坏旧模型 | 既有工件不可恢复 | v3 只读迁移、v4 method envelope、双版本 fixtures；未知版本显式拒绝。 |
| 同模型的不同方法/数据共用 journal | trial 空间被污染或错误 resume | 保存不可变 study fingerprint，continue 逐字段校验；Qwen 使用独立 checkpoint 目录，冲突时保留旧文件并停止。 |
| GPU kernel 非位级确定 | 合理重放被错误判失败或跨环境 hash 漂移 | CPU 测试要求精确；双卡采用参数/分数容差并绑定完整环境，不承诺跨驱动 bitwise equality。 |
| audit 结果诱导改选候选 | 测试集再次变成验证集 | selection record 先写；audit 失败即本轮失败，下一研究轮必须预注册新协议/新 audit，不能沿用本轮成功声明。 |
| 双重用途与安全影响 | 更强拒答移除能力可被滥用 | 文档将其限定为授权研究；不自动上传模型，不将低拒答率表述为安全或正确性提升。 |

## 8. Implementation order and completion checklist

1. 先实现 `ara.py` 与纯 CPU 数学/hook/事务测试，冻结损失公式、数据 shape、抽样 manifest 和 adapter 初态语义。
2. 实现 Settings、AcceptanceGate、默认配置、研究/运行字段拆分及 validator，确保默认 directional 行为不变。
3. 实现 revision-aware Model 加载、kwargs 合并、模块 facade 与 balanced placement 预检；把本功能相关逻辑移出 `model.py`，运行 model 单测。
4. 实现 `trial_methods.py` 的版本化采样、统一 `apply_trial`、fingerprint、失败分类、候选锁定和 acceptance report 纯逻辑；`main.py` 只接线并提取等量旧 trial 分支。
5. 完成 staging adapter、独立 reload/audit、utils/reproduce v4 与三方 hash 绑定；覆盖超时、非零退出和非空目录错误路径。
6. 更新 README、默认配置与 Qwen 配置，跑全套静态检查、AST 尺寸检查和现有 checksum 集成测试。
7. 最后在真实 Qwen3.8-27B 上执行 120-trial validation gate；无 validation 候选时可按原协议追加到 200。锁定唯一候选后只执行一次 audit，并完成 adapter reload 复测；仅当 `acceptance.json` 为 passed 才称任务完成。

完成时应逐项确认：所有新配置出现在 `--help`；旧配置无需修改；directional checksum 未变；CARA trial 可 checkpoint/resume/reapply/export/reproduce；calibration/validation/audit 无行重叠；真实模型达到 audit Keywords/KL、显存、稳定性与 reload 复评分门槛；acceptance/reproduce/adapter hash 一致。任何尚未运行的真实模型实验必须标为 pending，不得用 validation、PR #211 或第三方的 4/100 报告代替。

## 评审结论

**有条件通过。** v2 已直接修复全部 P1 设计问题，未遗留 P0/P1 文档缺口；当前栈在锁定的 Transformers/PEFT/bitsandbytes 版本上具备实现该方案的必要接口，范围也被限制在量化基座上的 rank-128 CARA-LoRA，而非无约束全矩阵 ARA。

通过条件仅是实施与实证门禁，不是待补设计项：下游必须按本规格完成自动化测试且保持 directional checksum；在指定双 RTX 3090 环境运行预注册的 calibration/validation/audit 协议；最终 `acceptance.json`、reproduce.json 与 adapter hashes 一致并通过全部 audit/reload 阈值。若 200 trials 后无 validation 候选，或唯一候选在 audit/reload 失败，应如实判定本功能实证验收失败，保留 journal 和 Pareto 结果，不得降低阈值、改 marker 或引用第三方成绩宣称通过。

## 实施过程发现的方案缺陷

- 现有通用 `Score`/paired score record 没有独立的样本数元数据；只有 `KeywordRate` 的 `md_display`/`rich_display` 暴露 `x/n`，`KLDivergence` 记录无法仅凭分数对象证明实际行数。由于本方案明确禁止修改 scorer/evaluator，本实现对 Keywords 分母执行严格校验，并以锁定 commit/split 的有效 scorer 配置、study fingerprint 与独立 audit 初始化约束 KL 数据身份和行数。若未来允许演进 scorer contract，应加入机器可读 `sample_count` 与 dataset fingerprint，再由 gate 同时验证两项指标；在此之前，KL 的 100 样本断言属于配置约束而非 score record 自证。
- 文件变更表要求 `main.py` 承载新增的重放、审计、staging 导出和独立 reload 编排，同时又要求该文件净增行数不大于零；这些职责无法在不继续扩大既有超长入口模块的情况下保持清晰边界。因此实现新增 `src/heretic/workflow.py`，只迁移/承载高层编排辅助函数，`main.py` 仍是唯一 CLI 入口且净行数未增加；该拆分不改变功能范围或依赖关系。
