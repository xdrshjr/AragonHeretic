# 文献来源与主张边界

## 核验协议

- 访问日期：2026-09-01。
- 只采用作者论文页、arXiv、会议论文集、DOI/出版商和 OpenReview 等一手页面。
- 会议论文存在时，以会议元数据确定年份、卷期、页码和 DOI；方法细节以论文正文或补充材料为准。
- “可支撑”表示来源直接陈述、给出公式或报告实验；“不可支撑”表示需要新的受控实验或源码审计。
- 本文件是审计材料，可以保留精确 URL；正文应通过 BibTeX 引用，不应展示仓库、分支或变更请求的原始标识。

## 来源登记

| ID | 主来源与版本 | 状态 | 可支撑的主张 | 不可支撑的主张 | 定位锚点 |
|----|--------------|------|----------------|----------------|----------|
| Piras2026 | https://ojs.aaai.org/index.php/AAAI/article/view/40551；细节版 https://arxiv.org/html/2511.08379v2 | AAAI 2026，Vol. 40(39)，32728--32736，DOI 10.1609/aaai.v40i39.40551 | SOM-MD 的公式、算法、参数、数据、模型范围，以及论文报告的 ASR | 源码矩阵法的效果；跨协议公平比较；对未测模型的泛化；HarmBench 验证集与 159 条测试提示是否独立 | AAAI 元数据；arXiv v2 §3.2、Alg. 1、§4.1、Tables 1--2、Appendix B.1--B.4 |
| Arditi2024 | https://proceedings.neurips.cc/paper_files/paper/2024/hash/f545448535dfde4f9786555403ab7c49-Abstract-Conference.html | NeurIPS 2024，Vol. 37，DOI 10.52202/079017-4322 | 单方向拒答介导、13 个开源聊天模型、最大 72B、方向消除与添加的因果干预 | 所有模型或所有风险类别都严格一维；多方向/矩阵法的相对优劣 | Proceedings 摘要；论文方法与附录 |
| Wollschlager2025 | https://proceedings.mlr.press/v267/wollschlager25a.html；版本核对 https://arxiv.org/abs/2502.17420v2 | ICML 2025，PMLR 267:66945--66970；arXiv v2 更新于 2026-02-08 | 梯度式方向识别、概念锥、多个独立拒答方向、正交不等于干预独立 | SOM 的局部原型更优；源码矩阵法具有独立机制 | PMLR 摘要与 BibTeX；论文 §§4--6 |
| Pan2025 | https://arxiv.org/abs/2502.09674v4 | arXiv v4；页面注明 ICML 2025 accepted | Llama 3 8B 安全微调位移的多维 SVD 分析、主方向和次级方向解释 | 可直接作为通用拒答消融基线；跨模型普适性 | arXiv 摘要与 Comments |
| Park2024 | https://proceedings.mlr.press/v235/park24c.html | ICML 2024，PMLR 235:39643--39666 | 线性表征的形式化、探针/控制联系、非欧氏内积的重要性 | 具体拒答行为一定满足其全部假设 | PMLR 摘要与 BibTeX |
| Zou2025 | https://arxiv.org/abs/2310.01405v4 | arXiv v4，更新于 2025-03-03 | 表征工程作为群体表征层面的读出和控制框架 | 拒答的一维或多维结构；源码矩阵法的正确性 | arXiv 摘要与版本记录 |
| Turner2023 | https://arxiv.org/abs/2308.10248v5 | arXiv v5，更新于 2024-10-10 | Activation Addition 从提示对的中间激活差构造控制向量 | 拒答消融效果；任意模型的无副作用控制 | arXiv 摘要与版本记录 |
| Rimsky2024 | https://aclanthology.org/2024.acl-long.828/ | ACL 2024，pp. 15504--15522，DOI 10.18653/v1/2024.acl-long.828 | CAA 对样本对差分求平均，并在推理时加入残差流 | 方向删除、SOM 组合或矩阵权重优化的效果 | ACL Anthology 摘要与导出 BibTeX |
| Mazeika2024 | https://proceedings.mlr.press/v235/mazeika24a.html | ICML 2024，PMLR 235:35181--35224 | HarmBench 的标准化自动红队框架，18 种方法与 33 个目标模型/防御的比较 | 任意单一判别器在所有分布上无偏；SOM 数据划分的独立性 | PMLR 摘要与 BibTeX |
| Xie2025 | https://openreview.net/forum?id=YfKNaRktan | ICLR 2025 conference paper | SORRY-Bench 的细粒度风险分类、语言/格式变体与自动评估器 | Piras 等抽取 4,000 条后的具体采样逻辑；矩阵法训练数据 | OpenReview 论文正文与元数据 |
| Kohonen2013 | https://doi.org/10.1016/j.neunet.2012.09.018 | Neural Networks 37:52--65 | SOM 用规则网格原型表示分布并保持邻接拓扑 | 拒答表征必然构成 SOM 可忠实恢复的流形 | 出版商摘要与 DOI |
| Bergstra2011 | https://proceedings.neurips.cc/paper/2011/hash/86e8f7ab32cfd12577bc2619bc635690-Abstract.html | NeurIPS 2011，Vol. 24 | TPE/序贯模型优化的技术背景 | 512 次试验足以得到 SOM 方向组合的全局最优 | Proceedings 摘要与论文 |
| Akiba2019 | https://doi.org/10.1145/3292500.3330701 | KDD 2019，pp. 2623--2631，DOI 10.1145/3292500.3330701 | Optuna 软件框架及其 define-by-run 接口 | Piras 等特定搜索预算的充分性或全局最优性 | ACM KDD 正式 DOI 与论文 |
| Snoek2012 | https://proceedings.neurips.cc/paper_files/paper/2012/hash/05311655a15b75fab86956663e1819cd-Abstract.html | NeurIPS 2012，Vol. 25 | 贝叶斯优化用于昂贵黑盒目标 | 特定离散排列空间中的收敛保证 | Proceedings 摘要与论文 |

## SOM-MD 关键事实提取

| Claim ID | 精确事实 | 来源定位 | 写作限制 |
|----------|----------|----------|----------|
| C-LIT-001 | 候选方向为 $r_i=w_i-\nu$，其中 $w_i$ 是有害表征 SOM 神经元，$\nu$ 是无害表征质心 | Piras2026, Alg. 1 lines 3--5；arXiv HTML §3.2 | 不写成“两个 SOM 的差” |
| C-LIT-002 | 使用六边形拓扑 $4\times4$ SOM，共 16 个候选神经元 | Piras2026, §4.1 MD | 不写成最终同时消融 16 个方向 |
| C-LIT-003 | 搜索 $k=2\ldots7$；$k\leq3$ 用 128 次，$k>3$ 用 512 次试验 | Piras2026, §4.1 MD；Appendix B.2 | 这是预算，不是穷举保证 |
| C-LIT-004 | Optuna TPE 搜索先执行总预算 25% 的随机试验，再进入 TPE 建议阶段 | Piras2026, Appendix B.2, Alg. 2；Akiba2019 | 不把 TPE 与整个 BO 概念混同；软件实现引用 Akiba2019 |
| C-LIT-005 | 投影按 $\Pi_{r_1}\circ\cdots\circ\Pi_{r_k}$ 顺序复合；搜索空间按 $\binom{16}{k}k!$ 计 | Piras2026, Eq. 7c；Appendix B.2 | 非正交方向时保留排列顺序；标准函数复合记号的实际施用方向需结合实现，本审计不另行定义左右顺序 |
| C-LIT-006 | 4,000 个 SORRY-Bench 有害提示用于 SD 有害质心和 SOM；6,000 个 Alpaca 无害提示用于质心；论文将 159 个 HarmBench standard 提示称为测试提示，并另称 BO 在 HarmBench 验证集上运行 | Piras2026, §4.1 Datasets and MD；Appendix B.2 | 不称 159 条为独立留出测试集；公开文本未说明验证集与测试提示的划分或重合关系 |
| C-LIT-007 | 目标包括 7 个安全对齐模型和 1 个带 RR 防御的 Mistral-7B-RR | Piras2026, §4.1 Models and Judge | 不写成“8 个同质基础模型” |
| C-LIT-008 | 判别器为 HarmBench-Llama-2-13B-cls，指标为有害请求被判为遵从的 ASR | Piras2026, §4.1 Models and Judge | ASR 高表示安全机制被更强地绕过，不表示系统更安全 |
| C-LIT-009 | 论文报告 SOM/方向构造只需数分钟，但单 GPU 的 BO 搜索平均约 8 小时 | Piras2026, Appendix B.4 | 不将局部构造成本等同于完整自动搜索成本 |
| C-LIT-010 | 论文在其协议下报告 MD 对 SD/RDO 的 ASR 在全部 8 个目标设置中更高 | Piras2026, Table 1 and discussion | 必须加“该文报告”；不能外推到源码矩阵法 |

## 不可比性检查表

| 比较轴 | SOM-MD | 源码矩阵法 | 当前可否给出数值结论 |
|--------|--------|------------|----------------------|
| 决策变量 | 候选方向的有序子集 | 连续模块映射或低秩适配器参数 | 否 |
| 内层证据 | 最后提示 token 的目标层激活 | 模块输入/输出样本与邻域损失（需代码审计） | 否 |
| 外层目标 | 验证集 ASR | 多目标评分（需代码审计） | 否 |
| 干预位置 | 同一方向算子跨层作用 | 被选择模块的权重映射 | 否 |
| 数据与模型 | Piras2026 固定协议 | 尚无同协议公开受控实验 | 否 |
| 搜索预算 | 128/512 个方向组合试验 | 实现配置依赖 | 否 |

结论：当前文献只允许建立机制差异、提出实验假设和审计源码主张，不允许建立性能排序。

## 未决项

- [UNCERTAIN] Piras2026 未公开说明 BO 所称 HarmBench 验证集与 159 条 standard 测试提示之间的划分比例、样本标识或重合关系。当前证据不能确认存在独立留出测试集，也不能排除方向选择对报告结果的选择偏差。
- [UNCERTAIN] Pan2025 的 arXiv 页面注明 ICML 2025 接收，但尚未在本轮检索中定位到 PMLR 正式条目。正式排版暂用 arXiv v4，提交前再核验。
- [UNCERTAIN] Piras2026 从 SORRY-Bench 获得 4,000 个有害提示的具体抽样与增强组成需要结合其开源代码进一步核查；论文正文只明确给出总数。
- [UNCERTAIN] 源码矩阵法是否在目标线性层存在偏置、是否在部署时保持与优化目标相同的行归一化，属于代码与实现审计问题，不由本文件裁定。
