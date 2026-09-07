# 从多方向到无显式方向：任意秩拒答消融的形式化、适配与失败实验分析

本目录是中文 IEEE conference、Letter 双栏论文。修订将上游 ARA 原型的
源码形式化、本地 point-v1 适配及 2026-09-04 的失败验收实验分别陈述。
本地 trajectory-v2 只作为已实现的后续协议讨论，指定归档没有其效果结果。

归档中 120/120 trial 为 COMPLETE；最低 Keywords 为 0.54，对应零基编号
trial 66、首 token KL 为 0.08997529745101929。所有 120 个候选满足 KL
上限，但没有候选通过联合效果门槛，acceptance.json 为 failed，
没有选定或导出的 adapter。Keywords 是词法代理，不是人工语义拒答标注。

## 重算证据并构建

以下命令从仓库根目录依次执行；任一步失败时先修复，再继续下一步。
证据提取器使用 Python 3.11+ 标准库，不导入模型、Heretic 或 Optuna。

```powershell
python -B docs/papers/heretic-ara/evidence/summarize_ara_v1.py
python -B -m unittest discover -s docs/papers/heretic-ara/evidence -p test_summarize_ara_v1.py
python -B docs/papers/heretic-ara/evidence/summarize_ara_v1.py --check
powershell -NoProfile -ExecutionPolicy Bypass -File docs/papers/heretic-ara/build.ps1 -Clean
```

提取器默认路径由脚本位置推导，换工作目录不影响默认输入。可用
`--archive-dir PATH` 和 `--paper-dir PATH` 重定位工作区内相同归档及输出；
不允许越界、与原始归档重叠或选择不同研究。`--check` 只读比较四个生成
文件，不创建目录、不修复旧结果。实验验收 failed 是有效观测，提取成功
仍返回 0；损坏、缺失或不一致输入及过期生成文件返回 1，参数用法错误返回 2。

写入前先校验九个原始文件及清单身份，再暂存整组字节，逐文件原子替换。
这不是四文件原子事务；任何中途替换错误必须重新运行生成命令，并让整组
`--check` 通过后才能编译。不要手改生成的 JSON、CSV 或表格来修补结果。

构建可选 `-ToolchainDirectory PATH` 显式指定 TeX 工具目录，兼容
`-Clean` 和 `-Open`。本次执行不使用 `-Open`。脚本预检 Python、XeLaTeX、
BibTeX、kpsewhich、pdftotext、pdfinfo、pdffonts、pdftoppm 与 pgfplots，
然后执行 XeLaTeX → BibTeX → XeLaTeX → XeLaTeX。显式目录优先，
其次已安装的 MiKTeX/TeX Live，最后 PATH；编译不交互安装宏包。

## 交付与验收记录

入口为 [paper.tex](paper.tex)，本地交付为 [paper.pdf](paper.pdf)。
最终构建身份和逐页验收详见
[修订评审记录](paper-output/review-logs/ara-v1-revision-review.md) 与
[构建报告](paper-output/revision-check/build-report.json)。
构建脚本成功只说明编译和已执行的机器检查通过；最终接受还要求文本、
元数据、字体及每页视觉检查通过，且它们绑定同一个最终 PDF SHA-256。
重建或修改 PDF 后须重新检查，不能复用旧报告。

2026-09-07 节点 #3 最终复核并重新构建，验收通过：13 页，814833 字节，33/33 字体嵌入；
23 项证据测试通过，39 个标签和 20 个引用键均解析，全部页面已视觉检查。
PDF SHA-256：
`8BF84FCFFDCBE635BFE14D905C0DC225BFE681B6A4191053447AFCF8FAF1E253`。
工具链为 MiKTeX 25.12（XeTeX 4.16 / BibTeX 4.2），本地 Python 3.14.6。
最终日志无排版溢出、引用、缺字或字体替换错误；三条精确匹配的版本兼容
提示及一处经视觉检查接受的 Underfull vbox 已记录。
最终评审修正了上游行归一化保护分支中零初始行的边界措辞；公式、
本地适配算法和实验统计不变。勘误记录在修订溯源中。

本机 Poppler 24.04.0 存在 Adobe-GB1 中文资源查找缺陷。构建按方案
实施缺陷 I01 动态核实相关字体资源后，以已有 PyMuPDF 1.28.2 完成
字体/文本及第 1–12 页渲染；pdfinfo 和第 13 页 pdftoppm 正常完成。
报告保留原工具失败记录和实际备用工具，不把失败改写成原工具通过。
没有安装新依赖；其他字体/语法错误仍会阻止构建。

## 证据和复现边界

- [修订溯源](evidence/ara-v1-revision-provenance.md) 给出三个完整源码版本、
  公式到函数映射、历史评分规则和运行工作树缺口。
- [确定性汇总](evidence/ara-v1-summary.json) 保存完整精度、原始哈希、
  实际协议、线性分位数、门槛和证据缺失。
- [全部 trial](evidence/ara-v1-trials.csv) 保留 120 个候选及十个 Pareto 点，
  供 [搜索图](figures/ara-v1-search.tex) 直接读取。
- [声明账本](evidence/claim-traceability.csv) 保持七列格式，含 C001–C036；
  E1–E5 含义不变，E6 为本地归档观察，E5 仍为未来实验。

这是一模型、一次自适应验证搜索；120 次评估不是独立重复实验。
关键词分母有逐 trial 计数证据，KL 的样本数只由配置和加载日志支持。
没有响应或 logits 可供重新评分，没有独立审计、重放或复载结果，
不能由小 KL 推断通用能力保持、语义安全或机制因果性。

记录 HEAD 未包含已部署但尚未提交的修复；后续修复提交也不被宣称为
运行工作树完整快照。v1 模型 revision 为 null，缺失的软件包版本不补造。
2 小时 38 分钟及约 52.05 GB GPU allocation、5.47 GB resident memory
来自日志，后两项是终端采样而非全程峰值。退出码 0 和 wrapper 的成功
文字不构成验收或导出成功证据。

`paper-output/analysis`、`debate`、`writing-plan` 和原 review logs 中的
2026-09-01 材料是旧稿历史记录，未改写成对本修订的评审。
旧九页 PDF、旧哈希和旧渲染不用于本次验收。后续提交应逐项包含论文源文件、
被引用的既有证据以及生成的小型 JSON/CSV/TeX；PDF、编译辅助文件、
本地文本报告和逐页 PNG 不暂存。源码提交范围包括方案及必要的历史证据依赖。
