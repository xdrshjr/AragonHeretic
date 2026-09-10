# SPDX-License-Identifier: AGPL-3.0-or-later
"""固定基座 teacher-forcing 轨迹上的完整词表 KL。"""

from __future__ import annotations

from dataclasses import dataclass

import torch

from .ara_refinement_capture import (
    TokenSequence,
    disabled_targets,
    evaluation_mode,
    generate_sequences,
    sequence_identity,
    snapshot_factors,
    tensor_identity,
)
from .ara_research_schema import ScoreIdentity


@dataclass(frozen=True)
class SequenceBundle:
    """每个角色独立的基座序列及 CPU 原始 logits。"""

    sequences: tuple[TokenSequence, ...]
    base_logits: tuple[torch.Tensor, ...]
    identity: ScoreIdentity


@dataclass(frozen=True)
class SequenceScore:
    """先 token 平均再 prompt 平均的 KL 与明确分母。"""

    value: float
    first_token_kl: float
    per_prompt: tuple[float, ...]
    identity: ScoreIdentity


def logits_on_sequence(model, sequence: TokenSequence) -> torch.Tensor:
    """同一前缀预测下一个基座 token；EOS 后没有位置。"""
    tokens = sequence.prompt_tokens + sequence.continuation[:-1]
    count = len(sequence.continuation)
    if not sequence.prompt_tokens or count < 1:
        raise ValueError("序列 KL 不能使用零有效位置")
    device = model.model.get_input_embeddings().weight.device
    inputs = torch.tensor([tokens], dtype=torch.long, device=device)
    with torch.inference_mode():
        outputs = model.model(
            input_ids=inputs,
            attention_mask=torch.ones_like(inputs),
            use_cache=False,
        )
        logits = outputs.logits[0, len(sequence.prompt_tokens) - 1 :]
        if logits.shape[0] != count:
            raise ValueError("序列 KL 因果位置数量不匹配")
        result = logits.detach().cpu()
    if not torch.isfinite(result).all():
        raise ValueError("序列 KL logits 非有限")
    return result


def prepare_sequence_bundle(model, prompts, identity: ScoreIdentity):
    """baseline 初始化显式禁用所有 adapter，并恢复调用前状态。"""
    with evaluation_mode(model), disabled_targets(model.ara_targets):
        sequences = generate_sequences(model, prompts, identity.prompt_ids, 32)
        logits = tuple(
            logits_on_sequence(model, sequence) for sequence in sequences
        )
    identity = identity.model_copy(
        update={
            "reference_sequence_hash": sequence_identity({"good": sequences}),
            "token_count": sum(len(row.continuation) for row in sequences),
        }
    )
    return SequenceBundle(sequences, logits, identity)


def masked_token_kl(
    base: torch.Tensor,
    current: torch.Tensor,
    mask: torch.Tensor,
    chunk_tokens=8,
) -> torch.Tensor:
    """FP32 分块计算 KL(base || current)，掩码位置不进入分母。"""
    if base.shape != current.shape or base.ndim != 2:
        raise ValueError("序列 KL logits shape 必须匹配且为二维")
    if mask.shape != base.shape[:1] or mask.dtype != torch.bool:
        raise ValueError("序列 KL 有效位置掩码不匹配")
    if not mask.any() or chunk_tokens < 1:
        raise ValueError("序列 KL 没有有效位置或分块大小非法")
    values = []
    for start in range(0, base.shape[0], chunk_tokens):
        selected = mask[start : start + chunk_tokens]
        left = base[start : start + chunk_tokens][selected].float()
        right = current[start : start + chunk_tokens][selected].float()
        if not torch.isfinite(left).all() or not torch.isfinite(right).all():
            raise ValueError("序列 KL logits 非有限")
        left = torch.log_softmax(left, dim=-1)
        right = torch.log_softmax(right, dim=-1)
        values.append((left.exp() * (left - right)).sum(dim=-1).clamp_min(0))
    return torch.cat(values)


def score_sequence_kl(model, sequence_bundle, options=None) -> SequenceScore:
    """同角色固定序列逐题评估，不跨 prompt 合并 token 分母。"""
    bundle = sequence_bundle
    options = options or {"chunk_tokens": 8}
    if len(bundle.sequences) != len(bundle.base_logits) or not bundle.sequences:
        raise ValueError("序列 KL 基座和题目数量不匹配")
    per_prompt, first = [], []
    with evaluation_mode(model):
        for sequence, base in zip(
            bundle.sequences, bundle.base_logits, strict=True
        ):
            current = logits_on_sequence(model, sequence)
            mask = torch.ones(len(sequence.continuation), dtype=torch.bool)
            values = masked_token_kl(
                base, current, mask, options["chunk_tokens"]
            )
            per_prompt.append(float(values.mean()))
            first.append(float(values[0]))
    identity = bundle.identity.model_copy(
        update={
            "candidate_identity": tensor_identity(
                snapshot_factors(model.ara_targets)
            ),
        }
    )
    return SequenceScore(
        sum(per_prompt) / len(per_prompt),
        sum(first) / len(first),
        tuple(per_prompt),
        identity,
    )
