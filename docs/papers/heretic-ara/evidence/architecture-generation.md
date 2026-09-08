# AI 架构图生成与核验

```json
{
  "sha256": "5b7b3be19297e51dbf2c0b06e9f43e59c737b3f7c3d207fcd0a4e4d5bb15bd94",
  "width_px": 1672,
  "height_px": 941,
  "tool": "image_gen.imagegen",
  "model": "未提供独立模型标识",
  "generated_at_utc": "2026-09-08T07:04:53.072856Z",
  "review_status": "passed",
  "seed": "未提供",
  "layout_width_mm": 160,
  "attempts": 2
}
```

本图使用内置 image_gen.imagegen 生成，并使用同一工具修正；没有使用 CLI、TikZ、Mermaid 或绘图代码代替生成。最终保留第二次工具输出的原始 PNG 字节，未缩放、重采样或覆盖文字。工具输出为 1672 × 941 像素，约 16:9；最终排版宽度 160 mm，约 265 个原生像素/英寸，清晰度以 PDF 页面检查为准，未修改 DPI 元数据。生成工具没有在结构化结果中提供独立模型、seed 或采样参数，这些字段不推测。

## 首次生成提示词（完整）

```text
Use case: infographic-diagram.
Asset type: architecture figure for a Chinese scientific paper; all in-image labels are short English.
Generate one publication-quality raster scientific flowchart, pure white background, landscape 16:9, ideally native width 2560 pixels or wider (minimum 1536), flat clean lines, large highly legible sans-serif type. Palette dark navy, blue and orange data streams, light grey dashed inset. No title, watermark, experimental numbers, checkmarks, complex formulas, gradients or 3D.
Layout must be scientifically precise. Two readable rows, ample whitespace:
TOP ROW: a left-to-right calibration preparation chain, each label exactly once: "Frozen base" -> "Clean module I/O" -> "Calibration bank". Two parallel blue/orange arrows between these boxes signify good/bad calibration inputs, with tiny labels "good" and "bad". Frozen base remains frozen.
MIDDLE ROW: a large thin rounded enclosing box labelled "Parameter search", containing "Local LoRA objective" -> "QR/SVD" -> "Validation". A downward arrow from Calibration bank feeds Local LoRA objective. A curved arrow labelled "metrics" from Validation returns to the Local LoRA objective inside Parameter search; this is the only feedback loop and its meaning is parameters resampled for the next candidate. No candidate failure ends this search loop.
A separate exit arrow from the right edge of Parameter search, labelled "budget exhausted", leads to "Final candidate gate" in lower row.
BOTTOM ROW: Final candidate gate is a decision box. Branch labelled "none eligible" -> "Stop without export" (bottom left). Branch labelled "select + lock" -> "Replay" -> an enclosing group "Export checks", with two side-by-side labels "Audit" and "Reload" inside it. Do NOT draw sequential arrows between Audit and Reload: their sequence differs by version. Group exit labelled "all pass" -> "Published adapter". A failure branch from Replay and a failure branch from Export checks must both route to the SAME Stop without export box with labels "fail". Avoid crossed arrows; use shared lower fail rail to the stop box. No Audit -> search arrow, no reselection, no bypass of checks.
Small grey dashed inset away from main arrows: heading "Trajectory-v2", underneath "Step-aligned capture", "Deployment gain", and italic "implementation only". The inset is a note about current code, not part of the measured run.
Prioritize correct readable labels and unambiguous arrows. No extra boxes or altered scientific meaning.
```

首图标签完整，数据流与搜索循环可读，但 select + lock 箭头绕过 Replay 直接进入 Export checks；Calibration bank 仅指向外层框。首图被拒绝作为交付图，未将这一错误以图注掩盖。

## 第二次纠错提示词（完整）

```text
Edit the provided scientific architecture figure. Preserve all typography, exact labels, overall layout, resolution, colors, boxes, inset, and all other arrows.
CRITICAL CORRECTION: the blue "select + lock" arrow from "Final candidate gate" must go into the TOP of the "Replay" box. Currently it wrongly goes into "Export checks". Erase that wrong downward arrowhead and reroute its horizontal segment leftwards to the x-position of Replay, then point down into Replay. The required only success path is Final candidate gate -> Replay -> Export checks -> Published adapter. There must be absolutely NO direct Final candidate gate -> Export checks edge. Keep the existing Replay -> Export checks arrow and both fail branches.
SECOND precise correction: the Calibration bank arrow currently terminates at the outer Parameter search frame. Continue its feed clearly within that box, with a thin blue route across the empty area above the inner nodes and an arrowhead into the top of Local LoRA objective. Keep this feed distinct from the metrics feedback loop below.
Do not change any text. Keep the grey Trajectory-v2 "implementation only" inset. No additions. Retain at least the original native 1672px width.
```

第二次生成保留全部标签及颜色，修复两条边。它是最终交付图片。首次输出标识 exec-c816fc07-b72a-437a-b98f-1a7f9b29930f，修订输出标识 exec-fd490165-aa71-4b11-9550-d3a6f613295c；输出标识来自工具保存提示，不当作模型 seed。

## 节点与边逐项核对

| 图中英文 | 中文含义与核查 |
| --- | --- |
| Frozen base | 冻结基座；good/bad 两路输入蓝/橙区分 |
| Clean module I/O | 在干净基座上采集模块实际输入和输出 |
| Calibration bank | 校准库；输入局部 LoRA 目标，无反向训练基座的边 |
| Parameter search | 包围候选循环；预算结束才进入最终候选 gate |
| Local LoRA objective | 模块内局部因子优化，不是全模型训练损失 |
| QR/SVD | 因子规范化；其数学和版本精度差异在正文给出 |
| Validation / metrics | 候选验证指标回流参数搜索；不表示审计集被搜索读取 |
| Final candidate gate | 搜索预算用尽后进行候选筛选；none eligible 指向停止 |
| Replay | select + lock 的唯一下一节点；通向 Export checks，失败停止 |
| Export checks / Audit / Reload | 通过重放后进行检查，Audit 与 Reload 不画统一顺序；无检查到搜索的反馈 |
| Published adapter | 只有 all pass 路径可以到达，不代表本次导出事实 |
| Stop without export | 无合格候选、重放失败、导出检查失败均结束发布 |
| Trajectory-v2 / Step-aligned capture / Deployment gain | 灰色虚线附注；明确 implementation only，不接入本轮实测数据流 |

两版图均逐个检查了文字。最终版所有成功路径均经过 Replay 与 Export checks，失败边不进入参数搜索；Audit 与 Reload 无先后箭头；无实验数字或通过勾号。边界与正文的对应关系：本轮 point-v1 在 120 个候选完成后的 Final candidate gate 停止，后续路径仅表示协议；v2 在新进程中重载暂存适配器并消费 audit，暂存与发布分开。

最终 PDF 版面上的架构图及相邻中文说明将在 evidence/review.md 的当前 PDF 全页检查中另行绑定，以上通过仅指图片本身的语义和原生像素核验。
