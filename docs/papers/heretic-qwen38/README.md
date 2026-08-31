# Heretic 与 Qwen3.8-27B 中文 IEEE 论文

本目录保存论文的可审查事实源。入口为 `paper.tex`，正文按章节拆分，图形由 TikZ 源码生成，公开案例数据保存在 `evidence/` 的 RFC 4180 CSV 中。`paper.pdf` 是构建工件，不是事实源。

## 构建状态

检查日期：2026-08-31。

| 项目 | 状态 |
| --- | --- |
| 论文源码 | 源码静态验收通过（19 个计划文件，13 个唯一 label，11 个有效交叉引用、对应 10 个唯一目标，10 个已解析引用键） |
| CSV 解析 | 通过（39 条 claim、2 条独立案例；表头、列数、证据枚举和主案例强制字段已核对） |
| `latexmk` / XeLaTeX / BibTeX | 当前环境未安装 |
| PDF 构建 | `not run` |
| `paper.pdf` SHA-256 | `not applicable`（尚未生成） |
| `git diff --check` | 已运行并返回 0；论文目录当前为未跟踪文件，另行执行的全文件尾随空白检查通过 |

当前状态仅表示源码交付，不能解读为 PDF 发布验收通过。获得可用的 TeX 环境后，仍须执行两次构建、日志检查、字体检查和 PDF 可搜索性检查。

静态验收还确认：所有 `\input` 路径存在；`\label` 唯一且每个 `\ref`/`\eqref` 均有目标；正文引用键与 `references.bib` 双向一致；两个 CSV 可由 Python `csv.DictReader` 以 UTF-8 和 `newline=''` 解析；计划目录内没有额外构建工件。当前 Git HEAD 为 `bedb94ef117a271532ac2058447fbc165d5051bd`。由于论文目录尚未被跟踪，`git diff --check` 不会读取其中的新增文件，因此另行对全部 19 个文件执行了尾随空白检查。

## 工具链

推荐使用完整 TeX Live 2026，并确保包含以下组件：

- `latexmk`、XeLaTeX 与 BibTeX；
- `IEEEtran` 文档类和 `IEEEtran.bst`；
- `xeCJK`、`fontspec`、`amsmath`、`booktabs`、`tabularx`、`hyperref`；
- PGF/TikZ 及 `arrows.meta`、`calc`、`fit`、`positioning`、`shapes.geometric` 库；
- FandolSong、FandolHei 与 FandolFang 字体。

当前机器没有 TeX 工具，以上版本目标尚未在本机验证。发布者应在构建日志中记录 `latexmk -v`、`xelatex --version`、`bibtex --version`、TeX Live 年份、`IEEEtran.cls`、`xeCJK.sty`、PGF/TikZ 与 Fandol 字体的实际版本。

## 离线构建

在本目录运行：

```text
latexmk -xelatex paper.tex
latexmk -xelatex paper.tex
```

第二次构建用于稳定交叉引用与参考文献。清理中间文件使用：

```text
latexmk -C paper.tex
```

发布验收还应执行：

```text
rg -i "undefined|Citation.*undefined|Reference.*undefined" paper.log
rg -i "Overfull \\[hv]box|Missing character|font.*substitut" paper.log
```

两个 `rg` 命令无匹配时返回退出码 1，这是预期结果，不代表编译失败。随后打开 `paper.pdf` 检查双栏布局、图表清晰度及中文字形，并确认文字可搜索、复制。最后记录：

```text
sha256sum paper.pdf
Get-FileHash -Algorithm SHA256 paper.pdf  # Windows PowerShell
```

## 常见故障

- **字体缺失**：确认 Fandol 字体已随 TeX Live 安装。Linux 可用 `fc-list | rg Fandol` 检查；Windows 可用 `fc-list` 或 XeLaTeX 日志定位缺字。
- **缺少宏包**：安装 TeX Live 的 `collection-latexextra`、`collection-langchinese`、`collection-pictures` 和 `collection-publishers`，或使用等价的完整安装。
- **引用未定义**：确认 BibTeX 实际运行，`references.bib` 可读，并再次执行 `latexmk -xelatex paper.tex`。
- **TikZ 失败**：确认 PGF/TikZ 及入口文件列出的五个库存在；不要把图源码单独用 pdfLaTeX 编译。

## 证据更新流程

论文项目固定到 Heretic 提交 `bedb94ef117a271532ac2058447fbc165d5051bd`，Qwen 官方卡固定到 `1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0`，主案例固定到 `a19c62b58f9b2cbaac9ab6343aeacef4442f3f94`，辅助案例固定到 `1d759df09209def0a089f5c9250130af82ebf41c`。更新来源时应：

1. 新增或替换明确的 commit URL，不把可变 `main` 页面当作冻结证据；
2. 先更新 `evidence/qwen38-case.csv` 和 `evidence/claim-traceability.csv`；
3. 再更新正文、表格和 `references.bib`，保持正文到证据、证据到正文双向可追踪；
4. 对未报告字段继续写 `not reported`，不得从其他案例回填；
5. 重新完成 CSV、引用、构建与 PDF 发布验收。

## 已知限制

- 主案例与辅助案例均为第三方模型卡报告，不是本文独立运行的 27B 实验。
- 当前仓库没有 Qwen3.8-27B 的本地或持续集成测试；论文只依据源码映射、官方架构说明与第三方记录讨论兼容性。
- 主案例没有报告运行时基础模型 revision、数据集 commit、完整 journal 或 BF16 合并模型复评分。
- 关键词拒答率和首 token KL 都是代理指标，不能证明总体能力、视觉能力或思考质量保持不变。
- 通用 `IEEEtran[conference]` 只提供 IEEE 会议版式；具体投稿语言、页限、匿名、伦理和版权要求必须以目标会议当年征稿说明为准。
