# 数学分析源码溯源记录

## 冻结版本

| 字段 | 值 |
|---|---|
| 上游仓库 | `https://github.com/p-e-w/heretic.git` |
| 分析引用 | `origin/ara` |
| 完整提交 | `edc3b123456c7f86f24d409b838ab3a7226e285e` |
| 提交作者时间 | `2026-08-17 14:57:09 +0530` |
| 提交主题 | `fix(ara): free gradient buffers after optimization (#426)` |
| 核验日期 | `2026-09-01` |
| 获取方式 | `git show origin/ara:<path>`，未切换工作树 |

精确复核命令：

```powershell
git rev-parse origin/ara
git show origin/ara:src/heretic/model.py
git show origin/ara:src/heretic/utils.py
git show origin/ara:src/heretic/main.py
git show origin/ara:src/heretic/config.py
```

## 证据编号与源码位置

| 编号 | 精确文件与行 | 已核验内容 |
|---|---|---|
| E01 | `src/heretic/model.py:578-683` | 全矩阵循环；原始行范数；`get_matrix`；冻结 I/O 搬运；$XW^\top$；良性 MSE；两项 kNN 距离；总损失；L-BFGS 参数；5 次外层 `step`；清梯度；归一化权重写回 |
| E02 | `src/heretic/model.py:684-798` | 低秩路径；基座权重/反量化；$W_0+BA$；损失内部完整行归一化；同构损失；优化 $A,B$；结束后仅清梯度 |
| E03 | `src/heretic/utils.py:238-243` | `torch.cdist(a,b)`；`topk(k, largest=False)`；对 $k$ 个距离求均值 |
| E04 | `src/heretic/model.py:57-71` | ARA 参数字段；模块 I/O 的层—组件—模块稀疏映射及张量语义 |
| E05 | `src/heretic/main.py:398-444` | 共同响应前缀检测与评估器初始化；缓存 I/O 所使用提示上下文的前置状态 |
| E06 | `src/heretic/model.py:934-1058` | 为每个目标模块注册钩子；取 `[:, -1, :]`；复制到 CPU；生成一个 token；两类提示分别聚合实际出现的稀疏模块，不保证跨类键集合相同 |
| E07 | `src/heretic/main.py:455-483` | ARA 路径只在试验循环前各采集一次良性/目标行为模块 I/O；方向基线走另一分支 |
| E08 | `src/heretic/main.py:501-548,625-645` | 外层搜索范围；构造参数；每试验重置/重载；调用全矩阵或低秩路径；随后模型级评分 |
| E09 | `src/heretic/model.py:164-168,181-228` | 低秩路径应用 PEFT；秩选择；`lora_alpha=lora_rank`；dropout 0；bias 不训练 |
| E10 | `src/heretic/config.py:191-217,235-242` | 默认目标组件；默认启用 ARA；低秩路径默认关闭；默认秩 128；默认行归一化为 FULL |

## 公式—源码逐项对应

| 公式或判断 | 直接证据 | 对应说明 |
|---|---|---|
| $X_gW^\top,X_bW^\top$ | E01:616-618 | `good_input @ matrix.T` 与 `bad_input @ matrix.T` |
| $\mathcal L_{good}=\|X_gW^\top-Y_g\|_F^2/(n_gq)$ | E01:620-623 | `(**2).mean()` 对二维张量所有元素取均值 |
| $d_k$ | E03:238-243 | 欧氏成对距离，选最小 $k$ 项，按邻居维求均值 |
| $\mathcal L_{pull}$ | E01:625-632 | 新目标输出到原良性输出的平均 kNN 距离 |
| $-\beta\mathcal L_{push}$ | E01:633-644 | `overcorrect_relative_weight * -mean_distances...` |
| $\lambda_g\mathcal L_{good}+\lambda_b\mathcal L_{steer}$ | E01:647-651 | 两个外层权重直接线性组合 |
| $\mathcal R(W)$ | E01:594-602,681-682 | 保存原行范数，在闭包和最终写回中使用归一化矩阵 |
| $W_{eff}=W_0+BA$ | E02:736-744 | 低秩有效权重及仅在损失内部执行的完整行归一化 |
| $\widetilde W(A,B)$ 的配置分支 | E02:737-748 | 非 FULL 时使用 $W_0+BA$；FULL 时损失使用 $\mathcal R(W_0+BA)$ |
| $\operatorname{rank}(BA)\le r$ | E02:721-739；E09:206-220 | 适配矩阵形状由 PEFT 秩给出；这是矩阵乘积的代数性质 |
| 低秩目标—部署不一致 | E02:741-744,775-798；E09:215-220 | 闭包用归一化有效权重，结束后无归一化结果写回；缩放为 1 |
| 冻结 I/O 局部代理 | E06；E07；E08 | 先一次性缓存，之后每试验重置并顺序编辑模块，未重采集 |
| 偏置未进入代理式 | E01:616-623；E02:746-753；E06:968-973 | 钩子存完整模块输出，重算只有矩阵乘法，无 `+ bias` |
| 稀疏模块跨类缓存前置条件 | E01:604-609；E02:728-729；E06:1001-1058 | 两条优化路径直接索引两类缓存的同一模块键；每类缓存只聚合该类批次中实际出现的键。两类键均存在后，E03 的 `topk` 还要求各自参考样本数不少于 $k$ |
| `topk` 复杂度边界 | E03:240-243 | 源码只固定 `Tensor.topk` 调用，未固定后端选择算法；使用 $T_{\mathrm{topk}}(a,b,k)$、$S_{\mathrm{topk}}(a,b,k)$ 记号而不声称具体渐近式 |

## 审计推论的证据等级

| 推论 | 等级 | 理由 |
|---|---|---|
| 动态 kNN 使拉近项一般非凸 | 数学推导 | $k=1$ 即多个凸距离函数的逐点最小值；由 E03 确认动态选择 |
| 固定邻居分段内可写为差分凸形式 | 数学推导 | 在邻居索引不变的分段内，拉近与推远距离均为凸距离和，负推远项给出凸减凸；该描述不扩展到动态邻居的全局目标 |
| 目标一般非光滑 | 数学推导 | 欧氏范数零点与邻居交换边界；由 E03 确认算子 |
| 完整行归一化是分段非线性映射 | 数学推导 | 常规区输出行范数为原范数，数值保护区输出可位于球内；不把全局像集等同于球面直积 |
| 全矩阵路径无显式更新秩约束 | 源码事实 | E01 直接以整个 `module.weight` 为参数 |
| 全矩阵更新实际秩为多少 | 未验证 | 源码不计算或约束更新秩，需要实验测量 |
| L-BFGS 通常 2--3 步收敛 | `[UNCERTAIN]` 注释性主张 | E01:667 只有注释，没有该提交内的统计证据或理论证明 |
| 低秩完整归一化差异会降低最终性能 | `[UNCERTAIN]` 实验问题 | 可由源码证明权重不一致，但性能影响需运行受控实验 |
| 目标组件在所有支持模型中均无偏置 | `[UNCERTAIN]` 架构问题 | 配置只给组件名称，未断言对应模块 `bias is None` |

## 完整性检查

- `git rev-parse origin/ara` 的输出与上表完整提交一致。
- 本记录引用的行号均按该提交中文件首行为 1 计数。
- 分析未依赖当前工作树中的项目代码内容，因此当前分支的未提交改动不会改变证据。
- 本记录只证明源码结构和数学推论，不证明模型级效果、跨模型泛化或相对基线优势。
