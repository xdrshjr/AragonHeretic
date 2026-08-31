# Heretic 原理与 Qwen3.8-27B 实践中文 IEEE 论文设计规格

**文档版本：v2**

**评审基线：Heretic `bedb94ef117a271532ac2058447fbc165d5051bd`；外部资料访问日期 2026-08-31**

## 评审记录

- **P0：无。** 未发现会使论文工程必然无法实施、或会导致危险数据/凭据处理的阻断项。
- **P1（已修复）—执行顺序与数值精度被过度概括。** 原稿把插件、评估数据、残差方向的加载/计算顺序混在一起，并把 CPU FP64 累计写成所有分支的共同性质。现已按 `main.py` 的真实顺序拆开，并区分普通均值路径与启用残差分析时的 FP32 全量路径。
- **P1（已修复）—复现工件范围不准确。** 本地 `save` 不会自动生成复现包；复现目录只在满足数据集/插件条件并上传 Hugging Face 时由当前用户流程生成，且 `reproduce.json` 不直接记录所选 trial 编号。现已修正流水线、接口和数据实体描述。
- **P1（已修复）—Qwen3.8 思考切点混淆。** 主案例的 `disable_thinking` 属于外部 Heretic WebUI 运行语境，不是当前仓库 `Settings` 的有效行为开关；当前默认 CoT 自动跳过也不能保证识别由 chat template 写入提示端的开放 `<think>`。现改为明确区分 WebUI 设置、当前 `response_prefix` 机制与辅助案例的精确闭合前缀。
- **P1（已修复）—公开配置契约含不存在或归属错误的字段。** `ScorerConfig` 使用 `optimization` 而非 `direction`；tokenizer/processor 没有独立 revision 字段；`direction_scope` 和 `direction_index` 是 trial 参数，不属于 `AbliterationParameters`。相关接口和数据模型已按源码改写。
- **P1（已修复）—多模态支持边界不足。** 当前 `Prompt` 仅含 system/user 文本，论文所述实验路径没有图像输入，也不改视觉塔或 MTP。现禁止把“可加载视觉语言模型”扩写为“已评估多模态消融”。
- **P1（已修复）—目标函数与低秩近似定义不完整。** 现明确 KL 为逐提示首 token 的 `D_KL(p_base || p_ablated)` 均值，并把 FULL 模式描述为固定随机种子的 `torch.svd_lowrank` 近似，而非精确截断 SVD 或无损范数保持。
- **P1（已修复）—外部证据不可冻结且主案例缺失基础模型 revision。** 三份模型卡现固定到具体 Hub commit；主案例没有报告其运行时基础模型 revision，必须写 `not reported`，不得用当前官方 revision 回填。
- **P1（已修复）—通用 IEEE 模板被误写成具体顶会合规。** 在未指定投稿 venue 时，6–8 页、中文摘要字数和匿名方式只能作为内部编辑目标；最终语言、页限和匿名规则仍以目标会议 CFP 为准。
- **P1（已修复）—构建与文件计划不闭合。** 当前评审环境没有 `latexmk`、`xelatex`、`bibtex` 或容器运行时；原稿又让 `main.tex` 生成 `paper.pdf` 并遗漏组件表文件。入口现统一为 `paper.tex`，补齐组件表，并把“源码验收”和“具备 TeX 环境后的 PDF 发布验收”分开记录。
- **P1（已修复）—验收规模过度。** 原稿强制两名审查者但没有对应执行资源，同时要求独立数据模型图却未进入图表/文件计划。现收敛为可追踪表、自动检查和下游独立复核，不再强制无增益的第三张图。
- **P2（已纳入下游核验）—细节可审计性。** 补充 winsorization 轴、关键词归一化、CSV 引号/编码、模型卡快照定位和 2026 年多方向拒答研究；这些不会阻断写作，但必须在成稿复核中保留。

## 1. Overview

本任务的最终交付物是一篇放在 `docs/` 目录下、采用 IEEE 计算机会议论文版式的中文技术论文。论文应以当前 Heretic 仓库的真实实现为唯一项目事实来源，解释该工具如何在不重新训练基础模型的情况下，自动发现并削弱 Transformer 语言模型中的拒答方向，同时用多目标优化约束行为变化。论文拟题为《Heretic：基于多目标优化方向消融的自动化大模型拒答抑制——Qwen3.8-27B 实践分析》。标题可以在排版阶段微调，但不得把 Heretic 描述成通用微调平台、内容审核产品或经过正式安全认证的系统。

目标读者是熟悉大语言模型基本概念、但未必了解机械可解释性、方向消融或 Optuna 的研究者与工程师。正文必须是通顺、易懂的简体中文；首次出现 `residual stream`、directional ablation、Pareto front、KL divergence、LoRA 等术语时同时给出中文解释和英文原词。论文既要说明方法原理，也要严格区分三类证据：当前仓库源码可直接验证的机制、外部论文或官方模型卡所陈述的背景事实、第三方模型卡报告但尚未由本仓库独立复现实验的数据。不得把第三方报告改写成作者自有实验结果。

本设计节点只规定论文内容、证据链、文件结构、排版和验收方法，不撰写最终论文，不修改 `src/heretic/`、测试、依赖或运行配置。下游实现的范围是新增 `docs/papers/heretic-qwen38/` 论文工程，并在具备指定 TeX 工具链时生成 PDF；不运行新的 27B 模型实验，不发布模型，不上传 Hugging Face，也不改变 Heretic 的 CLI 或算法。当前评审机器未安装 TeX 编译器，因此“源码完成”和“PDF 已编译验证”必须在 README 中分别记录，后者不得在未执行时声称通过。

### 1.1 论文中心论点

论文围绕以下可证伪命题组织：Heretic 将拒答方向的提取、作用层与强度的参数化、拒答率与行为保持之间的多目标搜索、以及可复现导出串成一条自动化流水线；对 Qwen3.8-27B 这类同时包含 Gated DeltaNet 与 Gated Attention 的混合架构，当前实现能够把两类注意力输出投影统一纳入消融，但默认思考模板、量化搜索与合并精度差异、多模态视觉路径和指标代理性仍会限制结论外推。

论文不能宣称“完全解除审查且不损失能力”“单一方向适用于所有拒答”“Qwen3.8-27B 的视觉、推理、编程能力已无损保留”，也不能将拒答降低等同于模型质量提高。结论应是范围受限的工程与机制分析，而不是安全承诺。

### 1.2 预期章节与篇幅

采用 `IEEEtran` 的 `conference` 模式，内部编辑目标为正文 6–8 页，不含必要附录时也应保持结构完整。这里的“IEEE 格式”只表示使用官方通用会议模板和 IEEE 引用样式；由于尚未指定目标会议，它不代表已经满足某一“顶会”的语言、页限、匿名、版权或 PDF eXpress 要求。正式投稿前必须用目标会议当年 CFP 覆盖这些内部目标。建议章节如下：

1. 摘要与关键词：摘要单段、自包含、不含引用/脚注/未展开缩写/公式，关键词 3–5 个；IEEE 的明确上限是 250 个英文单词，中文 350–500 字仅作为本项目内部篇幅目标，不能冒充 IEEE 的中文计数规则。
2. 引言：问题、现有人工 abliteration 的局限、Heretic 的贡献、Qwen3.8 案例价值。
3. 背景与相关工作：拒答方向、投影消融、LoRA、黑盒超参数优化；避免写成大段文献综述。
4. 系统与方法：残差采集、方向计算、模块发现、权重变换、行归一化、参数化层权重。
5. 多目标优化与可复现流程：KeywordRate、首 token KL、TPE、Pareto 试验、导出与复现包。
6. Qwen3.8-27B 实践分析：混合层映射、思考前缀测量边界、公开运行记录及限制。
7. 讨论、有效性威胁与伦理影响：代理指标、数据语言、量化误差、双重用途。
8. 结论。

## 2. Technical design

### 2.1 事实来源与证据优先级

论文写作前建立逐项 claim traceability 表。项目机制按以下优先级取证：`src/heretic/*.py` 与当前 Git 提交优先于本地索引，当前 `README.md` 和默认配置用于解释用户可见行为，历史版本博客或模型卡不得覆盖当前源码。外部架构事实优先引用 Qwen 官方模型卡；论文格式优先引用 IEEE Author Center；拒答方向理论引用 Arditi 等人的原始论文；Optuna 背景引用其 KDD 论文或官方文档。

本规格冻结以下审查快照，正文和证据表优先使用 commit URL 而不是可变的 `main` 页面：

- Heretic 源码：`bedb94ef117a271532ac2058447fbc165d5051bd`。
- Qwen 官方模型卡：`Qwen/Qwen3.8-27B@1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0`。
- 主案例模型卡：`sss22213/Qwen3.8-27B-Heretic-NoRefusal@a19c62b58f9b2cbaac9ab6343aeacef4442f3f94`。
- 辅助案例模型卡：`gjtgjt/Qwen3.8-27B-heretic@1d759df09209def0a089f5c9250130af82ebf41c`。

Qwen3.8-27B 的“实际使用情况”主要采用与当前仓库提交 `bedb94e` 对齐的主案例模型卡，并用辅助案例模型卡分析思考模板前缀问题。它们是可公开访问的叙述与汇总表，不是同行评审论文，也不是本仓库保存的原始 journal 或逐 trial 数据，统一标为 `third-party-report`。正文脚注或表注应逐项记录访问日期、来源快照 revision、已报告的基础模型 revision、Heretic 版本或 commit、硬件、量化方式、数据切分、试验数和局限；未报告字段写 `not reported`。主案例没有明确报告运行时基础模型 revision，不能用当前官方 revision 或辅助案例 revision 回填。来源不一致时只并列呈现，不求平均，也不挑选更好数字。

### 2.2 系统流水线

方法图应展示以下真实执行顺序，并在正文对应到当前实现模块：

1. `main.py` 解析 `Settings`；若未给 seed 则生成 seed，并由 `transformers.set_seed` 设置 Python、NumPy 与 PyTorch 随机状态。
2. `Model` 从同一个 `settings.model` 和可选 `model_commit` 加载 tokenizer、可选 processor 与模型，发现可消融模块并初始化零增量 LoRA；当前没有独立 tokenizer/processor revision 参数。
3. `main.py` 加载 good/bad 方向数据；自动探测 batch size，并在 `response_prefix` 未显式提供时尝试探测共同回答前缀。
4. `Evaluator` 随后加载 scorer 插件及各 scorer 自有的评估数据，并在尚未消融的模型上建立 baseline。方向数据与评估数据是不同角色，默认配置分别使用 train/test 切片。
5. `model.py` 通过一次 `generate(max_new_tokens=1, output_hidden_states=True)` 取得首次生成步的 hidden states，并抽取提示末位（用于预测首个新 token 的位置）。普通路径由 `get_residuals_mean` 逐 batch 在 CPU 以 FP64 累加再转 FP32；若启用 `print_residual_geometry` 或 `plot_residuals`，代码会物化 FP32 全量残差并直接求均值，不能把 FP64 累加写成所有模式共有的性质。
6. 对每一层计算有害与无害均值差，得到候选拒答方向；可选 projected abliteration 再去掉它在无害均值方向上的投影。
7. 模块发现逻辑定位每层注意力输出投影和 MLP 下投影；Qwen 混合层的两种注意力实现被映射为同一组件键。
8. Optuna 试验采样 `direction_scope`、`direction_index` 与每组件层权重核；每个试验先把 LoRA B 重置为零，再施加方向消融，随后由 scorer 计算目标。
9. multivariate TPE 按各 `ScorerConfig.optimization` 形成多目标 study，保留 Pareto 最优试验供用户选择，不引入隐藏加权总分。
10. 选定试验后可本地保存 LoRA adapter 或合并模型，也可上传 Hub。本地 `save` 路径不会自动生成复现包；当前交互流程仅在 Hub 上传、模型/数据集可固定、scorer 可复现且均为内置插件等条件成立时，可选生成并上传 `config.toml`、`requirements.txt`、`reproduce.json`、journal 副本和权重哈希。

论文必须把“方向发现数据”和“试验评估数据”写成不同切分，明确训练方向所用的 good/bad prompts 与评估拒答/KL 所用 prompts 不能在叙述中混为一谈。

### 2.3 数学定义

设层号为 `l`，有害提示集为 `B`，无害提示集为 `G`，模型开始生成回答时用于预测第一个响应 token 的提示末位残差向量为 `h_l(x)`。若启用 winsorization，代码是在求均值前，对每个“提示 × 层”向量沿 hidden component 维计算绝对值分位点并对称截断；它不是跨样本删除异常提示。论文使用如下符号，并与源码逐式核对：

\[
\mu_l^B=\frac{1}{|B|}\sum_{x\in B}h_l(x),\qquad
\mu_l^G=\frac{1}{|G|}\sum_{x\in G}h_l(x)
\]

\[
r_l=\operatorname{normalize}(\mu_l^B-\mu_l^G).
\]

当启用 projected abliteration 时，令 `g_l=normalize(μ_l^G)`，方向变为：

\[
\tilde r_l=\operatorname{normalize}\left(r_l-(r_l^\top g_l)g_l\right).
\]

全局 `direction_index` 为浮点数时，在相邻层的方向向量之间线性插值后再次归一化；为空时每层使用自己的方向。正文必须说明这不是把层号四舍五入，也不是在权重矩阵之间插值。

对于某组件 `c`、层 `l` 的权重矩阵 `W_{c,l}` 与本次选定单位方向 `v_l`，基础变换写为：

\[
W'_{c,l}=W_{c,l}-\lambda_{c,l}v_l(v_l^\top W_{c,l}),
\]

其中 `λ_{c,l}` 不是所有层相同的常数。对组件 `c`，令 `M_c=max_weight`、`p_c=max_weight_position`、`m_c=min_weight`、`d_c=min_weight_distance`，则当前实现的层权重核为：

\[
\lambda_{c,l}=\begin{cases}
M_c+\dfrac{|l-p_c|}{d_c}(m_c-M_c), & |l-p_c|\le d_c,\\
0, & |l-p_c|>d_c.
\end{cases}
\]

因此它在峰值位置取 `max_weight`，在支撑边界取 `min_weight`，边界之外不施加该组件的消融；当 `min_weight>0` 时边界处到区间外存在跳变。图示应明确画出支撑区间及该跳变，并注明不同组件独立采样。

LoRA 表达中，基础更新可以分解为 `B=-λv` 与 `A=v^T W`。在 `row_normalization=PRE` 时先做行归一化；在默认 `FULL` 策略中先构造完整调整后的权重，恢复原矩阵各行范数，再对差值执行固定 seed 的 `torch.svd_lowrank` 随机低秩近似并截取配置 rank，形成 adapter。论文要明确 FULL 模式是近似实现；有限 rank 时既不能写成无损正交投影，也不能声称最终 LoRA 合成矩阵严格保持每一行范数。

### 2.4 目标函数与搜索

默认拒答代理 `KeywordRate` 统计生成结果中出现预设拒答标记的提示比例；空回答也按命中处理。匹配前会转小写、移除 `*`、规范弯引号并折叠空白，但不会做语义分类。它是规则代理，英文默认标记对中文拒答并不充分。

令无害评估提示 `x` 上原模型首 token 分布为 `p_0(·|x)`，消融模型为 `p_θ(·|x)`。当前 `KLDivergence` 向 `torch.nn.functional.kl_div` 传入消融模型 log-probabilities 作为 input、原模型 log-probabilities 作为 target，因此目标是：

\[
\operatorname{KLScore}(\theta)=\frac{1}{|E_G|}\sum_{x\in E_G}
D_{\mathrm{KL}}\!\left(p_0(\cdot\mid x)\,\|\,p_\theta(\cdot\mid x)\right).
\]

实现使用 `reduction="batchmean"`，baseline 按定义返回零。正文必须写明 KL 的方向和首 token 范围，不能把“KL 较低”扩展为长文本分布、任务能力、正常思考质量或视觉能力不变。

搜索使用 Optuna multivariate TPE，包含 startup trials；每个 `ScorerConfig.optimization` 的 `minimize`、`maximize` 或 `none` 决定是否及如何进入 objective。默认目标是同时最小化 KeywordRate 与 KLDivergence，输出 Pareto 集。若论文绘制散点图，横轴、纵轴、样本量、量化模式和试验选择规则必须写清；没有逐 trial journal 时不得从截图或截断表格反推缺失数据。

### 2.5 Qwen3.8-27B 适配分析

固定的 Qwen 官方模型卡把 27B 基础模型描述为带视觉编码器的因果语言模型，其语言栈为 64 层稠密混合结构：16 个重复块，每块由 3 层 Gated DeltaNet 与 1 层 Gated Attention 构成；hidden size 为 5120，原生上下文为 262,144。以上只能标为官方声明。当前仓库没有 Qwen3.8-27B 的本地/CI 集成测试；兼容性证据由源码中的模块映射、共同的 `Qwen3_5` Transformers 架构形状和两份第三方模型卡共同构成，不能写成本文独立验证。论文需解释 Heretic 的适配点不是重新实现 Qwen，而是：

- `model.py` 把普通/门控注意力的 `self_attn.o_proj` 与线性注意力的 `linear_attn.out_proj` 都映射到统一的 `attn.o_proj` 组件，再同时处理 `mlp.down_proj`。
- 多模态模型通过语言模型层路径被加载和处理，但当前 `Prompt` 只有字符串 `system`/`user`，方向与 scorer 路径没有图像或视频输入。代码没有消融视觉编码器或 MTP，也不能因此声称图像理解、视频理解或 MTP 已验证。
- Qwen3.8 官方模板默认 thinking，开放 `<think>\n` 可能已经位于生成提示而不出现在解码的新 token 中；此时 Heretic 通过比较新生成回答得到的共同前缀不一定看得到 opener。当前默认 `chain_of_thought_skips` 的 `<think>` → `<think></think>` 规则也不等价于该模型卡记录的精确换行闭合形式。辅助案例因此显式把 `response_prefix` 设为 `"\n</think>\n\n"`，使残差和 KL 在 instruct 回答切点测量。论文优先展示 TOML 字符串，避免把 Bash 的 `$'...'` 语法误写成跨平台命令。
- 主案例模型卡所称 `disable_thinking = true` 是外部 Heretic WebUI 的运行设置，不是当前仓库 `Settings` 中会被 `Model.generate` 消费的参数。正文可报告该第三方设置，但不得把它列为当前 Heretic CLI 功能；当前源码内可验证的切点控制是 `response_prefix` 及其自动探测/CoT 跳过逻辑。

主要案例表转录第三方模型卡快照 `a19c62b...`：单张 RTX 5090 32 GB、Heretic `bedb94e`、350 个 Optuna trials（60 startup）、优化时为 bitsandbytes 4-bit 模型、最终在 CPU 上把 adapter 合并到 BF16 基础权重。模型卡报告基础模型在 100 条留出有害提示上 99/100 次关键词拒答，派生模型为 4/100，并报告 100 条无害提示上的首 token KL 为 0.0796。方向数据各为 400 条 harmful/harmless 训练样本，评估各为 100 条；模型卡称通过 WebUI 禁用 thinking 并添加了中文拒答标记。其基础模型 revision、数据集 commit、完整 journal 和 BF16 合并模型复评分均未报告或未随卡提供，相关字段必须写 `not reported`。这些数字必须以“模型卡报告”作主语，不能写成“本文实验表明”。同一表紧邻列出其局限：无 MMLU、GSM8K、编程、视觉或正常思考基准；关键词拒答与首 token KL 均是代理；4-bit 搜索对象和 BF16 合并交付物不是同一数值系统。

辅助案例快照 `1d759df...` 只用于解释模板边界和提供另一份第三方兼容性观察：它记录 Heretic 1.4.0 在 Qwen3.8 的 64 个混合层中统一处理 Gated Attention 与 DeltaNet 输出投影，并通过显式 `response_prefix` 跳过思考开场。其明确报告的基础模型 revision `1d4bf0f...`、200/60 试验、参数和 98→27/100、KL 0.0446 等结果都只能属于辅助案例，不得与主案例拼接成一个“综合实验”。

### 2.6 图表设计

正文至少包含两图两表：

- 图 1：从提示数据、残差方向、多目标搜索到 adapter/merge/reproduce 的端到端流水线，所有箭头可在源码中追踪。
- 图 2：层权重核和矩阵投影示意，分别标注 direction index、组件、层位置与 `λ`。
- 表 1：Heretic 当前实现组件与职责，包含 `main.py`、`model.py`、`evaluator.py`、`plugin.py`、`scorer.py`/内置 scorers、`config.py`、`utils.py`、`reproduce.py`。
- 表 2：Qwen3.8-27B 第三方案例证据卡，分列“事实”“来源类型”“复现状态”“限制”。

图形使用 LaTeX/TikZ 或可审查的矢量 PDF，禁止使用无法编辑且来源不明的网络图片。数值图只能由 `evidence/` 下的结构化数据生成；如果公开来源没有逐 trial 数据，就只做汇总表，不伪造 Pareto 曲线。

## 3. File and module change plan

下游论文实现仅新增以下文件，不改动项目代码：

| 路径 | 责任与内容 |
| --- | --- |
| `docs/papers/heretic-qwen38/README.md` | 构建命令、TeX 版本、字体要求、来源更新方法与已知限制。 |
| `docs/papers/heretic-qwen38/paper.tex` | `IEEEtran` conference 入口、中文字体配置、作者占位信息、章节装配、合规声明；入口名与 PDF 名一致。 |
| `docs/papers/heretic-qwen38/latexmkrc` | 固定 XeLaTeX/BibTeX 构建流程；不依赖个人绝对路径。 |
| `docs/papers/heretic-qwen38/sections/00-abstract.tex` | 中文摘要和 3–5 个关键词。 |
| `docs/papers/heretic-qwen38/sections/01-introduction.tex` | 问题、贡献、边界。 |
| `docs/papers/heretic-qwen38/sections/02-background.tex` | 拒答方向、方向消融、Optuna 相关工作。 |
| `docs/papers/heretic-qwen38/sections/03-method.tex` | 残差提取、方向、投影、行归一化和低秩实现。 |
| `docs/papers/heretic-qwen38/sections/04-optimization.tex` | scorer、TPE、多目标 Pareto 和复现流程。 |
| `docs/papers/heretic-qwen38/sections/05-qwen38-case.tex` | 混合架构映射、思考切点和公开实践案例。 |
| `docs/papers/heretic-qwen38/sections/06-discussion.tex` | 有效性威胁、伦理与双重用途、复现限制。 |
| `docs/papers/heretic-qwen38/sections/07-related-work.tex` | 精简且直接相关的工作比较。 |
| `docs/papers/heretic-qwen38/sections/08-conclusion.tex` | 有边界的结论与未来验证。 |
| `docs/papers/heretic-qwen38/figures/pipeline.tex` | TikZ 流水线图源码。 |
| `docs/papers/heretic-qwen38/figures/weight-kernel.tex` | 层权重核与矩阵投影图源码。 |
| `docs/papers/heretic-qwen38/tables/components.tex` | Heretic 源码组件、职责与论文 claim 定位表。 |
| `docs/papers/heretic-qwen38/tables/qwen38-case.tex` | Qwen 案例证据表。 |
| `docs/papers/heretic-qwen38/evidence/qwen38-case.csv` | 从公开模型卡逐字段转录的数据与 URL，不混入推断值。 |
| `docs/papers/heretic-qwen38/evidence/claim-traceability.csv` | claim id、论文位置、证据类型、文件/URL、版本、复核状态。 |
| `docs/papers/heretic-qwen38/references.bib` | IEEE、Heretic、Qwen、Arditi、Optuna 和案例来源的 BibTeX。 |
| `docs/papers/heretic-qwen38/paper.pdf` | 由 `paper.tex` 生成的发布工件，不是事实源；是否提交二进制由仓库维护者决定，但 README 必须记录实际构建状态与已构建文件的 SHA-256。 |

`paper.tex` 不应包含大量正文，章节文件应能够独立审查。作者、单位、邮箱等信息在未获提供时使用明确的匿名投稿占位方式，不虚构姓名或机构；该占位不代表任何具体 venue 接受匿名投稿，正式交付前由维护者按 CFP 替换。

## 4. Interface design

### 4.1 论文工程接口

论文工程只提供本地构建接口，无 REST、WebSocket、数据库或远程服务。标准命令为 `latexmk -xelatex paper.tex`，清理命令为 `latexmk -C paper.tex`。构建必须在论文目录执行并直接产出 `paper.pdf`，不使用构建后重命名。所有图表从版本化的 `.tex`/`.csv` 输入构建；引用网页只在更新证据时需要联网，已冻结的论文源码应可离线排版。

XeLaTeX 负责中文排版，`xeCJK` 默认使用 TeX Live 自带或 README 明确安装的开源 CJK 字体，并给出 `fc-list`/XeLaTeX 缺字诊断。参考文献使用 `IEEEtran.bst` 与 BibTeX。模板保持 IEEE 双栏、标题、摘要、关键词、图表题注和参考文献样式；不得手工压缩字号、负间距或页边距来满足页数。README 必须列出经验证的 TeX Live、`IEEEtran`、`xeCJK`、TikZ 和字体版本；本机缺少工具时写“未编译”，不能以肉眼审阅替代编译结果。IEEE Author Center 要求应在验收清单中引用，而不是只凭外观模仿。

### 4.2 被论文描述的 Heretic 用户接口

论文中出现的命令只作为现有系统接口说明，不新增 CLI。最小使用模式是 `heretic <model>`；消费已有复现描述使用 `--reproduce <reproduce.json>`；独立评估同时提供基础 `--model` 和 `--evaluate-model`；思考切点使用当前 `--response-prefix`/`response_prefix`；导出由 `model_action` 与 `export_strategy` 控制。复现材料的生成是满足约束后的 Hub 上传选项，不能描述成每次本地保存的必然产物。下游写作时必须从当前 `config.py` 的 `Settings` 与 Pydantic kebab-case CLI 行为复制准确名称，不从旧模型卡或 WebUI 猜测参数。

公开配置契约至少解释以下组：`model`/`model_commit`（tokenizer 与 processor 跟随同一仓库和 revision，没有独立标识）、`dtypes`/`quantization`/`device_map`、good/bad 与 scorer-owned evaluation `DatasetSpecification`、`scorers[].plugin` 与 `scorers[].optimization`、`orthogonalize_direction`、`row_normalization`、`full_normalization_lora_rank`、`n_trials`/`n_startup_trials`、`response_prefix`/`chain_of_thought_skips`、`model_action`/`export_strategy`/上传设置。`direction_scope`、`direction_index` 和每组件四个核参数是 Optuna trial 参数，不是 `Settings` 顶层配置字段。`disable_thinking` 不得作为当前 Heretic CLI 参数展示。示例不得包含真实令牌、私有路径或 Hugging Face 凭据。

### 4.3 错误与边界表达

论文工程构建失败时应返回非零退出码并在 README 列出字体、缺包、BibTeX 引用和 TikZ 四类常见诊断。论文描述 Heretic 失败模式时，仅引用代码可验证的异常或公开案例限制，不制造错误码。对 unsupported pure state-space 架构、找不到投影模块、显存不足、数据集字段不匹配和版本不兼容，使用“能力边界”表述，不承诺自动恢复。

## 5. Data model

### 5.1 Heretic 配置与运行实体

论文用简短表格或正文说明以下现有实体即可，不强制增加一张数据模型图；字段名称以固定 commit 源码为准：

- `Settings`：聚合模型加载、方向数据、消融、优化、评估、导出与复现消费设置；CLI、环境变量和 TOML 最终归并到这一 Pydantic 对象。
- `DatasetSpecification`：`dataset`、可选 `commit`、`split`、`column`、prefix/suffix 与 system prompt；good、bad 和各 scorer 的 evaluation 角色分离，不存在独立 subset 字段。
- `AbliterationParameters`：只含某组件的 `max_weight`、`max_weight_position`、`min_weight`、`min_weight_distance`。`direction_scope` 只用于采样，解析后以 `direction_index` 浮点值或 `None` 单独传给 `Model.abliterate`。
- `ScorerEntry`/`Score`：插件实例、`ScorerConfig.optimization`、baseline 与当前分数；objective 名称、值和方向由同一 `_objective_entries()` 顺序导出。
- Optuna trial/study：原始采样参数、objective values、`direction_index`/转换后核参数/score records 等 user attributes、状态与 JSONL journal；Pareto trials 由多目标 study 派生。
- 复现包：`reproduce.json` v3 含 timestamp、可选 system、environment、`Settings.model_dump()` 可序列化字段（标记 `exclude=True` 的运行时字段不在其中）、所选参数与 scores、上传权重 hashes，但不直接含 trial 编号；同目录另有 requirements、config、README、可选 SHA256SUMS 和 journal 副本。当前用户流程只在符合条件的 Hub 上传时调用生成逻辑。

不得给当前项目虚构关系数据库 schema。以上是内存对象、配置文件与 JSON/journal 工件的逻辑关系。

### 5.2 论文证据实体

`claim-traceability.csv` 每行包含 `claim_id,section,claim_text,evidence_class,source,source_revision,accessed_at,locator,verification_status,notes`。`evidence_class` 只能是 `code`、`project-doc`、`official-external`、`peer-reviewed`、`third-party-report` 或 `author-inference`。推断必须在正文用“据此推断”表达。

`qwen38-case.csv` 至少包含 `case_id,base_model,base_revision,heretic_version,hardware,search_precision,export_precision,trials,startup_trials,direction_good_n,direction_bad_n,eval_harmful_n,eval_harmless_n,base_refusals,derived_refusals,first_token_kl,thinking_mode,language_markers,source_url,source_revision,accessed_at,limitations`。空缺写 `not reported`，绝不补零；主案例的 `base_revision` 必须保持 `not reported`。两个 CSV 均使用 UTF-8、单行表头和 RFC 4180 引号规则，含逗号/换行的 claim 或 limitations 必须正确加引号。所有公开数字须能回指固定模型卡快照的具体标题、表格行或段落。

### 5.3 引用记录

`references.bib` 的网页条目包含作者/组织、标题、commit URL、访问日期和版本；软件引用包含 commit 或 release。Arditi 使用 NeurIPS 2024 正式论文元数据，Optuna 使用 KDD 2019 与 DOI `10.1145/3292500.3330701`。Qwen 架构使用固定官方模型卡，IEEE 格式规则使用 IEEE Author Center。Hugging Face 第三方模型卡可以引用，但文献类型和正文措辞必须揭示其未经过同行评审。相关工作还应至少纳入一篇对单方向假设作扩展/挑战的近期同行评审工作，避免把 2024 结论写成定律。

## 6. Testing and acceptance criteria

### 6.1 结构与构建验收

1. 最终论文位于 `docs/papers/heretic-qwen38/`，除论文文件外不修改源代码、测试或默认配置。
2. 源码验收不依赖本机 TeX：所有计划文件存在，CSV 可由 Python `csv` 模块解析，内部引用路径有效，`git diff --check` 通过，README 明确记录工具链探测结果。
3. PDF 发布验收必须在安装了 README 指定工具链的环境中执行：`latexmk -xelatex paper.tex` 连续两次均成功，无 undefined references、missing citations、越界的 overfull box 或字体缺字/回退警告；`paper.pdf` 可打开且文字可搜索复制。未运行时状态只能写 `not run`，不得判定为通过。
4. PDF 使用 IEEE conference 双栏版式；6–8 页是内部目标而非 venue 规则。摘要为单段、自包含且关键词 3–5 个，图表和参考文献编号连续；投稿时再按目标会议 CFP 验证页限、语言与匿名要求。
5. README 给出完整依赖、离线构建步骤和常见故障，不依赖作者机器绝对路径；若生成 PDF，记录 SHA-256，并明确它对应的源码 commit 和 TeX 版本。

### 6.2 技术准确性验收

1. 实施者从 `main.py`、`model.py`、`evaluator.py`、内置 scorer、`utils.py` 与 `reproduce.py` 反向核对方法图、全部公式和执行顺序，下游 Review 节点独立抽查；每个关键 claim 在 traceability 表有文件与行号/符号定位。
2. 残差方向的正负号、首响应 token 测量位置、全局浮点方向插值、per-layer 模式、projected 方向、分段线性权重核以及 FULL 行归一化后的低秩近似均与当前源码一致。
3. KeywordRate 的空回答/字符串规范化规则、默认关键词局限、`D_KL(p_base || p_ablated)` 的首 token 与 baseline 定义、多目标 `optimization` 和 Pareto 选择均准确，不能用“总体能力无损”代替实际指标。
4. Qwen 混合层映射明确区分 `self_attn.o_proj` 与 `linear_attn.out_proj`；文本 Prompt、视觉编码器、MTP、thinking 模式和纯状态空间模型的未覆盖范围不被隐藏，且不声称仓库已独立运行 27B 集成测试。
5. 主案例的 99/100、4/100、0.0796、350/60、400/400、100/100、4-bit 搜索与 BF16 合并逐项回查快照 `a19c62b...`；基础模型 revision 写 `not reported`，相邻文字出现“第三方模型卡报告”和局限。辅助案例不得贡献主案例数值。
6. 复现流程明确区分本地保存、Hub 上传、可选复现包和 `--reproduce` 消费路径，不虚构本地自动 manifest 或 trial 编号字段。

### 6.3 写作与证据验收

1. 全文为通顺的简体中文，缩写首次展开；每段有明确主题句，不堆砌源码名，也不使用宣传性“革命性”“零损失”“彻底解除”等措辞。
2. 项目事实、官方声明、同行评审结果、第三方报告和作者推断在 traceability 表与正文措辞中可区分。
3. 所有外部数字、架构参数、格式规则和理论结论均有邻近引用；不存在无法打开的 URL、孤立 BibTeX 条目或引用未使用。
4. 下游 Review 节点执行“从引用到原文”和“从正文到引用”的双向抽查；公开材料未提供的数据保持 `not reported`。
5. 伦理段明确方向消融具有双重用途，可能削弱安全训练；论文只做透明、可审计的研究说明，不提供规避服务控制、部署滥用系统或隐藏模型修改来源的操作指南。

### 6.4 最小验收命令

下游实现可使用以下只读检查，并将结果写入 README 或 CI 日志：

```text
latexmk -xelatex paper.tex
latexmk -xelatex paper.tex
rg "undefined|Citation.*undefined|Reference.*undefined" paper.log
rg "third-party-report|bedb94e|0.0796|99/100|4/100" evidence sections
python -c "import csv, pathlib; [list(csv.DictReader(p.open(encoding='utf-8', newline=''))) for p in pathlib.Path('evidence').glob('*.csv')]"
git diff --check -- docs/papers/heretic-qwen38
```

第三条 `rg` 检查预期无匹配；应人工核对其退出码语义，不把“无匹配”的退出码 1 当成编译错误。若由于提交策略不纳入 PDF，发布验收仍应在具备工具链的环境完成并把 PDF SHA-256 写入 README；若工具链不可用，则仅可完成源码验收并明确剩余发布条件。

## 7. Risks and mitigations

### 7.1 科学有效性

- **拒答并非必然是单一方向。** Arditi 等人在 NeurIPS 2024 给出强证据，但 2026 年已有同行评审工作提出多方向拒答抑制。缓解方式是在相关工作与限制中呈现证据范围和后续扩展，不把单方向假设写成定律。
- **代理指标效度有限。** KeywordRate 可漏掉隐式拒答，也会误判引用关键词的正常回答；首 token KL 不覆盖完整序列。缓解方式是限定主张，并把语义拒答分类、完整序列散度和任务基准列为未来验证。
- **数据语言与分布偏差。** 默认数据主要为英文，案例虽补充中文关键词，也不等于中文能力评估。缓解方式是记录语种与标记集合，禁止跨语言泛化结论。
- **公开案例不可独立复现。** 主要数字来自第三方单机运行，主案例没有基础模型 revision、数据集 commits、完整 journal 或 BF16 复评分。缓解方式是固定来源快照、对缺失字段写 `not reported`，并明确本文是实践分析而非复现实验报告。

### 7.2 工程与版本

- **快速演化的 Qwen 与 Transformers 接口。** 混合层属性或 chat template 可能随 revision 改变；当前 `get_model_class()` 的架构探测也没有接收 `model_commit`，即使后续 tokenizer/model 加载使用了 revision，仍不应宣称所有网络探测都完全冻结。缓解方式是固定论文证据快照和运行配置，记录这一源码边界，并在真正复现实验前验证解析到的 config/model class。
- **量化搜索与 BF16 合并的偏差。** 4-bit 副本上选择的 Pareto trial 未必在 BF16 交付模型上保持相同分数。缓解方式是案例表并列两种精度，并把 BF16 复评列为必要的后续实验。
- **思考模板污染测量。** 开放 `<think>` 位于提示模板时，自动共同前缀检测可能无法观察到它；错误切点会让思考内容主导残差与 KL。缓解方式是分别记录外部 WebUI thinking 设置、当前 Heretic 的精确 `response_prefix`、最终渲染提示和测量切点，禁止把 non-thinking 结果推广到 thinking 推理。
- **多模态与混合组件覆盖不完整。** 当前数据/Prompt 路径是文本，语言层适配不等于视觉路径、MTP 或所有辅助头均被验证，仓库也没有 Qwen3.8-27B 集成测试。缓解方式是列出处理与未处理模块及证据等级，避免架构全覆盖措辞。
- **论文排版环境差异。** 中文字体和 IEEE 模板版本会造成构建不一致，且本评审环境没有 TeX 引擎或容器运行时。缓解方式是固定 TeX Live、模板和开源字体版本，区分源码/发布验收，并在可用环境记录完整构建日志和 PDF 散列。

### 7.3 安全、伦理与合规

Heretic 的目标会直接削弱模型拒答机制，具有明确双重用途。论文应将其定位为研究对齐脆弱性、可解释性和模型行为控制的工具，提醒使用者遵守模型许可、数据许可、机构伦理规范与适用法律。案例只报告公开、聚合的拒答指标，不重印有害提示全文，不给出面向真实服务绕过检测的操作手册。论文同时说明：降低过度拒答可能改善合法任务可用性，但降低恰当拒答也可能扩大危险能力暴露；两者必须通过下游安全评估而非单一 refusal score 判断。

### 7.4 交付风险

最大的文档风险是把仓库实现、旧版本行为和第三方模型卡拼成一条看似统一的实验叙事。缓解措施是强制 claim traceability、版本固定、案例隔离、独立审查和逐项验收。任何无法追溯到当前源码或明确外部来源的技术细节都应删除或标为待验证假设，不能靠“常见做法”补齐。

## 8. Authoritative references for downstream writing

下游写作至少使用并核验以下来源：

- Heretic 当前仓库：`README.md`、`config.default.toml`、`src/heretic/main.py`、`model.py`、`evaluator.py`、`config.py`、`utils.py`、`reproduce.py` 与内置 scorers，统一固定到 `bedb94ef117a271532ac2058447fbc165d5051bd`。
- IEEE Author Center, [“Authoring Tools and Templates”](https://conferences.ieeeauthorcenter.ieee.org/write-your-paper/authoring-tools-and-templates/) 与 [“Structure Your Paper”](https://conferences.ieeeauthorcenter.ieee.org/write-your-paper/structure-your-paper/)，用于版式、摘要、关键词、方法、结果和引用要求。
- Arditi et al., [“Refusal in Language Models Is Mediated by a Single Direction”](https://proceedings.neurips.cc/paper_files/paper/2024/hash/f545448535dfde4f9786555403ab7c49-Abstract-Conference.html), NeurIPS 2024，用于拒答方向理论来源；不得把其 13 个模型结论直接当作 Qwen3.8 实验。
- Akiba et al., [“Optuna: A Next-generation Hyperparameter Optimization Framework”](https://doi.org/10.1145/3292500.3330701), KDD 2019，用于优化框架背景；Heretic 使用的 sampler 选项和搜索空间仍以 `bedb94e` 源码为准。
- Piras et al., [“SOM Directions Are Better than One: Multi-Directional Refusal Suppression in Language Models”](https://doi.org/10.1609/aaai.v40i39.40551), AAAI 2026，用作单方向假设的近期扩展/反证背景，不把其方法误写为 Heretic 当前实现。
- Qwen 官方 [`Qwen/Qwen3.8-27B@1d4bf0f`](https://huggingface.co/Qwen/Qwen3.8-27B/blob/1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0/README.md) 模型卡，用于 64 层、混合架构、上下文与 thinking 参数等官方声明。
- [`sss22213/Qwen3.8-27B-Heretic-NoRefusal@a19c62b`](https://huggingface.co/sss22213/Qwen3.8-27B-Heretic-NoRefusal/blob/a19c62b58f9b2cbaac9ab6343aeacef4442f3f94/README.md) 模型卡，用于主实践案例；基础模型 revision 记为 `not reported`。
- [`gjtgjt/Qwen3.8-27B-heretic@1d759df`](https://huggingface.co/gjtgjt/Qwen3.8-27B-heretic/blob/1d759df09209def0a089f5c9250130af82ebf41c/README.md) 模型卡，用于混合层与精确 `response_prefix` 的辅助案例。

引用网页时保存访问日期；所有模型卡使用上述 commit URL。若写作期间官方资料或仓库实现发生变化，应新建勘误记录并说明是否重开技术核验，而不是静默替换本规格基线。

## 评审结论

**有条件通过。** 本规格在可行性、完整性、一致性和范围控制四个维度已完成修复，未留存未解决的 P0/P1 问题。下游实施必须满足以下发布条件：

1. 论文中的源码事实固定到 Heretic `bedb94e`，外部 Qwen/案例事实固定到本规格列出的三个 Hub commit；主案例基础模型 revision 保持 `not reported`。
2. 第三方模型卡数据只能写成公开实践报告，不得改写为本文或本仓库的独立 27B 实验；不得补造 journal、BF16 复评分、多模态或 thinking-mode 结果。
3. 当前环境只能完成论文源码验收。只有在具备记录版本的 XeLaTeX/BibTeX 工具链后实际完成两次构建、日志检查、PDF 可搜索性检查与 SHA-256 记录，才可把交付状态升级为“PDF 发布验收通过”。
4. 若确定具体 IEEE 会议，必须再次按该 venue 当年 CFP 核对语言、页限、匿名、伦理声明、版权和 PDF 合规要求；通用 `IEEEtran[conference]` 不能替代这一步。

## 实施过程发现的方案缺陷

1. **PRE 行归一化的因子描述不够精确。** 固定提交在 PRE 模式下先令 $\widehat W=D_s^{-1}W$，再设置 $A=v^\top\widehat W$、$B=-\lambda D_s v$；因此基础式 $B=-\lambda v,A=v^\top W$ 只直接对应无行归一化路径。论文已按源码补全该缩放关系，没有把 PRE 写成对基础式的简单前置归一化。
2. **连续峰值位置未必由实际层取得。** `max_weight_position` 是连续浮点 trial 参数，而模型层号是整数。层权重核在连续位置 $p_c$ 理论取 $M_c$；只有 $p_c$ 恰为整数时，实际层才严格取得该最大值。论文图与正文已区分连续核峰值和离散层采样。
3. **复现包的基础模型 revision 仍有覆盖风险。** `create_reproduce_folder()` 会查询当前 Hub 模型信息并覆写 `settings.model_commit`。若一次运行显式加载旧 revision，复现包可能记录生成时的 Hub HEAD，而不是实际加载 revision。论文把它列为版本冻结边界，并建议分别保存与核对两者；本节点不修改项目算法代码。
