# Heretic–ARA 中文研究稿

题目为《面向语言模型拒答行为干预的校准式任意秩消融：方法适配与实验分析》。
入口是 `paper.tex`，本地 PDF 输出为 `paper.pdf`。

本稿的唯一量化来源是 `docs/logs/ara-v1/` 中的 point-v1 归档。
120 个候选全部完成，最低拒答关键词命中率为 54/100，对应首 token
KL 为 0.089975，但没有候选通过验收。Heretic 同协议结果、trajectory-v2
实测、独立 audit/reload 分数及精确运行工作树快照均缺失。
源码中的 v2 机制只构成实现证据，不构成效果验证。

## 环境与构建

使用独立 Python ≥3.11 和 `matplotlib==3.11.0`，以及已有 XeLaTeX、BibTeX、
pdftotext、pdftoppm、kpsewhich。宏包需要 ctex、Fandol、amsmath/amssymb、
booktabs、tabularx、graphicx、hyperref、geometry 和 setspace。
正常命令不会安装依赖、下载模型或执行研究项目。缺少组件时命令明确失败；
`requirements-paper.txt` 只描述论文绘图依赖，不修改项目根环境。

以下命令在仓库根目录逐条执行；脚本也支持从其他工作目录调用。

```powershell
python -B docs/papers/heretic-ara/evidence/summarize_ara_v1.py --check
python -B docs/papers/heretic-ara/evidence/verify_paper.py --check-sources-only
powershell -NoProfile -ExecutionPolicy Bypass -File docs/papers/heretic-ara/build.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File docs/papers/heretic-ara/build.ps1 -Finalize
```

正常构建先校验 AI 图，再重算数据和统计图、执行数值测试及来源检查，随后
XeLaTeX → BibTeX → XeLaTeX → XeLaTeX，并重新提取当前 PDF 文本、渲染全部页面。
成功时 `build/build-manifest.json` 的状态是 `pending_review`。
实施者逐页检查 `build/pages/page-*.png` 后，按下一节记录复核，再运行 `-Finalize`。
只有 Finalize 成功、manifest 为 `success` 才表示论文交付完成。

`-CheckOnly` 只检查来源与衍生制品；`-Clean` 仅清理指定 `build/paper.*`
中间文件，并且只可用于正常构建。`-CheckOnly` 与 `-Finalize` 互斥，均不可
与 `-Clean` 组合。`-PythonExe` 可指定 Python；`-TexBin` 可指定现有 TeX
二进制目录。工具发现顺序为显式目录、PATH、用户 MiKTeX、标准 MiKTeX/TeX Live。

缓存和临时文件均置于 `build/`，包括 MiKTeX 用户配置、格式缓存、日志及
独立 Fontconfig 缓存；已有安装目录仅提供可执行程序、宏包和字体。
MiKTeX 自动安装与 shell escape 显式禁用。
此配置依据 [MiKTeX 配置文档](https://docs.miktex.org/manual/miktex.ini.html)
及其[官方环境变量使用示例](https://hub.docker.com/r/miktex/miktex/dockerfile)。

本机实际预检发现 `zhnumber`、`geometry`、`setspace` 未安装；本次显式从
CTAN 准备到 `build/texmf/tex/`，通过进程 `TEXINPUTS` 使用。原有 MiKTeX
Poppler 24.04 无法读取已安装的 Adobe-GB1 映射，因此显式准备了官方
[Xpdf tools 4.06](https://www.xpdfreader.com/download.html) 的 Windows
64 位 `pdftotext.exe`、`pdftoppm.exe` 到 `build/xpdf/`。
构建发现此目录时使用这些 PDF 工具，并以 `build/xpdfrc` 显式指向已有
MiKTeX 的 Poppler 字符映射。Xpdf 的 120 dpi PPM 通过 Matplotlib 依赖的
Pillow 无缩放转换为 PNG；最终页面像素保持不变。

本次显式准备命令与组件哈希保存在 `build/environment-preparation.json`。
Xpdf ZIP 的 SHA-256 为
`2b6ca45da794e7854a6468fd6c8063fde62701f001ce03fa4f603eab7e15a0b6`。
新 checkout 若环境同样缺件，可从以下原始来源显式准备同一目录；这些操作
不在正常构建中自动执行：

- [zhnumber TDS 包](https://mirrors.ctan.org/install/macros/latex/contrib/zhnumber.tds.zip)：仅解压 `tex/latex/zhnumber/` 到 `build/texmf/`。
- [geometry 包](https://ctan.math.illinois.edu/systems/win32/miktex/tm/packages/geometry.tar.lzma) 和 [setspace 包](https://ctan.math.illinois.edu/systems/win32/miktex/tm/packages/setspace.tar.lzma)：解压各包 `texmf/tex/` 到 `build/texmf/tex/`。
- [Xpdf Windows 包](https://dl.xpdfreader.com/xpdf-tools-win-4.06.zip)：提取 `bin64/pdftotext.exe`、`bin64/pdftoppm.exe` 到 `build/xpdf/`。

所有解压目标须先解析并确认留在上述工作区路径；不修改系统工具目录。

## 复核与过期检查

`evidence/review.md` 的第一个 JSON 代码块必须含：

```json
{
  "status": "passed",
  "pdf_sha256": "当前 PDF 的 64 位 SHA-256",
  "page_count": 12,
  "render_dpi": 120,
  "reviewed_pages": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
  "reviewed_at_utc": "2026-09-08T00:00:00Z"
}
```

示例中的页数、时间、哈希必须替换为实际复核值。正文记录数学、中文论述、
图表及每页版式检查；未解决的问题应标为 failed。复核由论文实施者完成。
正文、数据、图、来源或构建输入改变后必须重新构建并复核；仅修改 review
不会触发重编译。Finalize 比较完整 input_hashes、当前 PDF 和重新提取的文本，
旧 PDF、旧复核或任意同名文本均不能替代本轮检查。

`evidence/ara-v1-summary.json`、CSV、三个生成表格和统计图由生成器维护；
`--check` 只核对，不重写。统计图清单绑定其输入及 PDF/PNG 实际字节哈希。
source-provenance 记录历史 blob 与当前工作树的不同身份，claim-traceability
把论断绑定到章节 label、summary 字段、物理行或源码符号。

统计摘要中的 `analysis_base_revision` 固定记录本轮审查起点，当前方法文件
仍以逐字节 SHA-256 标识；实际构建时的 HEAD 写入本地 manifest 的
`checkout_head`。这样，单纯提交论文不会改变重算的实验统计。
TeX 验证和输入清单从 `paper.tex` 遍历实际 `input/include`，旧稿中未被
引用的章节不进入本稿。局部 `.gitattributes` 将 PNG/PDF 图形标为 binary，
防止根目录的强制文本换行规则在 Git 暂存或 checkout 时破坏图形字节。

```powershell
python -B -m unittest discover -s docs/papers/heretic-ara/evidence -p test_summarize_ara_v1.py
python -B docs/papers/heretic-ara/evidence/verify_paper.py --require-review
```

错误日志在 `build/logs/`，最终 TeX 日志为 `build/paper.log`，自动验证报告为
`build/verification.json`。构建失败将本轮 manifest 标为 failed；此前交付的
PDF 可以保留，但不能据其存在断言本轮成功。所有源文件和证据可提交，
根部 `paper.pdf`、构建缓存及页面预览仅在本地保留。
