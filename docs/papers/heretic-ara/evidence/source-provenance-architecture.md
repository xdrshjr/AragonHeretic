# Architecture Source Provenance

## Audit identity

- Audit time: `2026-09-01T02:16:04-05:00`
- Repository: `E:\TAKO-PROJECTS\heretic`
- Upstream pull request: `https://github.com/p-e-w/heretic/pull/211`
- GitHub web rendering inspected at: `2026-09-01T02:16:04-05:00`; cited PR page line numbers are capture-time rendering coordinates and may move as the discussion changes.
- Pull-request state observed from GitHub: draft, open, 19 commits, head branch `ara`
- Audited remote-tracking ref: `origin/ara`
- Audited head commit: `edc3b123456c7f86f24d409b838ab3a7226e285e`
- Audited head commit date/subject: `2026-08-17T14:57:09+05:30`, `fix(ara): free gradient buffers after optimization (#426)`
- Pull-request base SHA recorded by the PR: `96c7a7d98a4710903355f398cfd0ff1c65f55925`
- Local merge base of `origin/master` and `origin/ara`: `96c7a7d98a4710903355f398cfd0ff1c65f55925`
- Current local `origin/master` at audit: `bedb94ef117a271532ac2058447fbc165d5051bd`
- Diff from merge base to audited head: 5 files, 759 insertions, 171 deletions.
- Files changed: `src/heretic/config.py`, `src/heretic/evaluator.py`, `src/heretic/main.py`, `src/heretic/model.py`, `src/heretic/utils.py`.
- Method: static inspection with `git show origin/ara:<path>`; no checkout and no product-code modification.

## Exact source evidence

All line numbers below refer to the blobs at commit
`edc3b123456c7f86f24d409b838ab3a7226e285e`, not the working tree.

| Claim | Exact file and lines | Source reading |
|---|---|---|
| ARA parameters and sparse module-I/O structure | `src/heretic/model.py:57-71` | Dataclass holds layer interval, two outer weights, overcorrection weight and neighbor count; I/O is indexed by layer/component/module. |
| Full-matrix method iterates selected modules | `src/heretic/model.py:578-592` | Uses `[start_layer_index, end_layer_index)` and takes `module.weight` as the optimized tensor. |
| Full row-norm reparameterization | `src/heretic/model.py:594-602` | Caches original row norms and returns `row_norms * normalize(matrix)` when `FULL`. |
| Original I/O reused in local objective | `src/heretic/model.py:604-618` | Reads cached good/bad tensors and computes new outputs as `input @ matrix.T`. |
| Good-output MSE | `src/heretic/model.py:620-623` | Mean squared deviation from captured good output. |
| Pull-to-good and push-from-bad kNN terms | `src/heretic/model.py:625-645` | Adds distance to good outputs and negative weighted distance to original bad outputs. |
| Loss weights | `src/heretic/model.py:647-651` | Weighted sum of preserve and steer terms. |
| L-BFGS setup and loop | `src/heretic/model.py:653-669` | `lr=1`, `max_iter=20`, history 10, strong-Wolfe search, five `step` calls. |
| Full-matrix write-back and gradient cleanup | `src/heretic/model.py:674-682` | Clears gradients and copies the reparameterized matrix into the model. |
| LoRA rank selection and adapter construction | `src/heretic/model.py:181-228` | Uses configured ARA-LoRA rank and targets discovered leaf module names; `bias="none"` means PEFT does not train bias parameters, not that base modules have no bias. |
| Full versus LoRA reset semantics | `src/heretic/model.py:306-326` | Full-matrix ARA falls through to a base-model reload; LoRA fast reset zeroes only modules named `lora_B`, leaving `lora_A` unchanged. |
| Quantized base handling and FP32 samples | `src/heretic/model.py:699-734` | Dequantizes 4-bit weights if needed and moves cached samples as FP32. |
| LoRA effective matrix and row normalization | `src/heretic/model.py:737-748` | Objective evaluates `W_base + B @ A`, optionally normalized to cached row norms. |
| LoRA loss and optimizer | `src/heretic/model.py:750-798` | Reuses kNN loss, optimizes A/B with L-BFGS, then only clears gradients. |
| Module discovery | `src/heretic/model.py:355-425` | Finds text/multimodal layers and several dense, hybrid-attention and MoE output projections. |
| Hook capture | `src/heretic/model.py:934-999` | Registers all target hooks, saves last prompt-position input/output on CPU, generates one token, removes hooks. |
| Batched sparse aggregation | `src/heretic/model.py:1001-1058` | Unions module indices across batches and concatenates available I/O. |
| kNN definition | `src/heretic/utils.py:238-243` | Full Euclidean `cdist`, smallest-k selection per query, mean distance. |
| I/O captured once before trials | `src/heretic/main.py:455-460` | Good and bad module I/O are obtained outside the trial objective. |
| Outer ARA search space | `src/heretic/main.py:501-548` | Samples start/end layers, preserve weight, steer weight, overcorrect weight and k. |
| Per-trial reset, intervention and evaluation | `src/heretic/main.py:625-645`; `src/heretic/model.py:306-326` | Full-matrix mode reloads the base model. LoRA mode normally zeroes B only, so the model function returns to base while A carries over between trials. |
| TPE multi-objective study | `src/heretic/main.py:672-700` | Multivariate TPE, two minimize directions, journal-backed study. |
| Refusal heuristic and baseline | `src/heretic/evaluator.py:22-48,50-96` | Stores baseline refusal count; treats empty responses or configured substring matches as refusals. |
| Evaluation objective transforms | `src/heretic/evaluator.py:98-152` | Second objective is refusal count divided by baseline refusals (raw count if baseline is zero). KL mode uses `KL/scale` above the threshold and `refusal_score*target/scale` below it; PIQA mode returns negative `acc_norm`. |
| First-token distribution and KL direction | `src/heretic/model.py:1060-1086`; `src/heretic/evaluator.py:113-120` | Generates one token and computes `F.kl_div(current_logprobs, base_logprobs, log_target=True)`, i.e. $D_{KL}(p_{base}\|p_{edited})$. |
| Display Pareto front | `src/heretic/main.py:718-745`; `src/heretic/evaluator.py:131-150` | Sorts stored raw refusals and the stored quality attribute. The latter is raw KL in KL mode and negative `acc_norm` in PIQA mode; display reverses the PIQA sign. |
| ARA and target-component defaults | `src/heretic/config.py:191-243` | `attn.o_proj` and `mlp.down_proj`; ARA true; ARA-LoRA false; rank 128; full row normalization. |
| Trial defaults | `src/heretic/config.py:266-274` | 200 total trials, 60 startup trials. |
| Prompt defaults | `src/heretic/config.py:385-423` | Configuration points to external datasets, using 400/400 optimization slices and separate 100/100 evaluation slices. The configuration alone does not establish long-term public availability. |

## Pull-request statements versus implementation evidence

| PR statement/status | Location in PR page | Audit treatment |
|---|---|---|
| Hooks capture per-module I/O and direct matrix optimization avoids explicit refusal directions. | PR page lines 170-178 as rendered on 2026-09-01 | Supported at the architectural level by source; effectiveness is not established by static inspection. |
| Objective described as “affine-convex” and L-BFGS as typically converging in 2-3 iterations. | PR page lines 179-180 | Not accepted as verified. Dynamic kNN selection, the negative distance term, optional row normalization and LoRA bilinear factorization contradict a blanket convexity reading. No convergence log or test is committed in the inspected diff. |
| Memory requirements described as barely above regular ablation. | PR page line 179 | Not accepted as a general bound. One device matrix is optimized at a time, but all captured module I/O are cloned to CPU; actual peak memory depends on model, prompt count, dimensions, dtype and activated experts. |
| Demonstration result on one model. | PR page lines 181-185 | Treated as upstream anecdotal/demo evidence, not a controlled ARA-vs-SOM experiment. |
| PR remains draft. | PR page lines 130-145 | Method is a research prototype at the audited state. |
| ARA-LoRA is the eventual merge route. | PR page lines 1125-1148 | Historical design signal only; the audited PR itself is not merged. |
| Row-normalized LoRA objective may differ from deployed mapping. | PR discussion lines 1188-1211 | Directly consistent with `model.py:737-744` and absence of a corresponding projection/write-back after `model.py:793-798`. |
| Gradient buffers caused multi-GiB VRAM retention and latest commit clears them. | PR page lines 1346-1354 | Source at `model.py:674-679` and `796-798` contains the cleanup. Magnitude is workload-dependent and was not reproduced here. |

## Architecture limitations and uncertainty log

1. **Frozen activation cache** — Proven: `main.py:455-460` is outside the trial objective, while mutations happen at `main.py:625-645`. Consequence: inner losses use original-model activations. The quantitative error introduced is `[UNCERTAIN]` without experiments.
2. **Sequential local edits** — Proven: `model.py:584-589` nests layer/component/module loops and writes each matrix before moving on. Later modules are optimized against cached rather than newly propagated inputs.
3. **Bias omission** — Proven: local output is `input @ matrix.T` (`model.py:616-618`, `746-748`). Whether captured output contains a nonzero bias is model-specific and therefore `[UNCERTAIN]` until each target module is checked.
4. **Module type assumption** — The implementation casts discovered modules to `Linear` (`model.py:590-592`, `696-697`). Compatibility with every `trust_remote_code` architecture is `[UNCERTAIN]`.
5. **Convexity** — `topk` changes neighbor membership (`utils.py:240-243`); the steer objective subtracts a distance (`model.py:639-644`); row normalization is nonlinear (`model.py:598-600`); LoRA uses bilinear `BA` (`model.py:737-739`). A blanket affine-convex claim is unsupported.
6. **Rank meaning** — Full-matrix mode has no explicit low-rank constraint, but that does not prove arbitrary/full rank of the learned delta. LoRA mode has `rank(delta W) <= r` and defaults to `r=128` (`config.py:214-217`).
7. **LoRA row-normalization mismatch** — In `FULL` mode, normalization is local to the objective (`model.py:741-744`); no projected effective matrix is encoded back into A/B after optimization (`model.py:792-798`). Runtime behavior could be affected by PEFT internals not shown here, so end-to-end magnitude is `[UNCERTAIN]`, while the source-level mismatch is concrete.
8. **LoRA trial-history dependence** — Fast reset zeroes B but retains A (`model.py:306-326`). Since BA is zero, the model function resets to the base mapping, but the non-convex A/B parameterization starts from an A inherited from the prior trial. Quantitative impact is `[UNCERTAIN]`.
9. **Bias semantics** — PEFT `bias="none"` (`model.py:215-223`) only disables bias training. The local proxy still omits an explicit base bias (`model.py:616-618`, `746-748`), whose existence is model-specific.
10. **Memory** — Captured tensors are CPU clones (`model.py:968-973`) for all target modules and both prompt classes. Exact host-memory growth is `[UNCERTAIN]` without model shapes and activation sparsity.
11. **Convergence and numerical safety** — Five L-BFGS calls are fixed (`model.py:667-669`, `792-794`), with no committed check for finite weights or logged stopping reason in the inspected code. Runtime stability is not established.
12. **Evaluation boundary** — String matching (`evaluator.py:50-96`) and first-token quality proxies with piecewise/scaled objectives (`evaluator.py:98-152`) are not comprehensive safety or capability measures.

## Review-driven corrections

- Full-matrix and LoRA reset paths are now distinguished: `model.py:306-326`.
- The actual TPE objectives are recorded rather than paraphrased as raw KL and refusal count: `evaluator.py:98-152`.
- The display-only Pareto attributes are distinguished from the TPE return values: `main.py:718-745`.
- `bias="none"` is recorded as a PEFT training policy, not a structural no-bias assertion.
- Dataset references are described only as external configured datasets; accessibility is not inferred from configuration.

## Reproduction commands

```powershell
git -C E:\TAKO-PROJECTS\heretic rev-parse origin/ara
git -C E:\TAKO-PROJECTS\heretic merge-base origin/master origin/ara
git -C E:\TAKO-PROJECTS\heretic diff --stat origin/master...origin/ara
git -C E:\TAKO-PROJECTS\heretic show origin/ara:src/heretic/model.py
git -C E:\TAKO-PROJECTS\heretic show origin/ara:src/heretic/main.py
git -C E:\TAKO-PROJECTS\heretic show origin/ara:src/heretic/config.py
git -C E:\TAKO-PROJECTS\heretic show origin/ara:src/heretic/evaluator.py
git -C E:\TAKO-PROJECTS\heretic show origin/ara:src/heretic/utils.py
```
