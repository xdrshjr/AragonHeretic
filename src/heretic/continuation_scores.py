# SPDX-License-Identifier: AGPL-3.0-or-later
"""Numerically stable conditional continuation scoring."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

import torch
from torch import Tensor

from .ara_trajectory import TrajectoryNonFiniteError


@dataclass(frozen=True)
class TokenizedContinuation:
    """One prompt/continuation pair with a verified suffix boundary."""

    prompt_index: int
    continuation_index: int
    token_ids: tuple[int, ...]
    suffix_start: int


def _token_ids(tokenizer: Any, text: str) -> tuple[int, ...]:
    encoded = tokenizer(
        text,
        add_special_tokens=False,
        return_attention_mask=False,
        return_token_type_ids=False,
    )
    values = encoded["input_ids"]
    if isinstance(values, Tensor):
        values = values.tolist()
    if values and isinstance(values[0], list):
        values = values[0]
    return tuple(int(value) for value in values)


def tokenize_continuations(
    tokenizer: Any,
    prompts: Sequence[str],
    continuations: Sequence[str],
) -> list[TokenizedContinuation]:
    """Tokenize pairs and prove prompt/suffix token boundaries are unchanged."""
    if not prompts or not continuations:
        raise ValueError("prompts and continuations must not be empty")
    records = []
    special_ids = set(getattr(tokenizer, "all_special_ids", ()))
    for prompt_index, prompt in enumerate(prompts):
        prompt_ids = _token_ids(tokenizer, prompt)
        if not prompt_ids:
            raise ValueError("rendered prompt tokenization must not be empty")
        for continuation_index, continuation in enumerate(continuations):
            normalized = continuation.strip()
            if not normalized:
                raise ValueError("continuations must not be empty")
            combined_ids = _token_ids(tokenizer, prompt + normalized)
            if combined_ids[: len(prompt_ids)] != prompt_ids:
                raise ValueError("prompt/continuation token boundary changed")
            suffix = combined_ids[len(prompt_ids) :]
            if not suffix:
                raise ValueError("continuation has no suffix tokens")
            if not any(token not in special_ids for token in suffix):
                raise ValueError("continuation contains only special tokens")
            records.append(
                TokenizedContinuation(
                    prompt_index=prompt_index,
                    continuation_index=continuation_index,
                    token_ids=combined_ids,
                    suffix_start=len(prompt_ids),
                )
            )
    return records


def _batches(
    records: Sequence[TokenizedContinuation],
    batch_tokens: int,
) -> list[list[TokenizedContinuation]]:
    batches: list[list[TokenizedContinuation]] = []
    current: list[TokenizedContinuation] = []
    width = 0
    for record in records:
        length = len(record.token_ids)
        if length > batch_tokens:
            raise ValueError("one continuation pair exceeds batch_tokens")
        next_width = max(width, length)
        if current and next_width * (len(current) + 1) > batch_tokens:
            batches.append(current)
            current = []
            width = 0
        current.append(record)
        width = max(width, length)
    if current:
        batches.append(current)
    return batches


def _model_device(model: Any) -> torch.device:
    device = getattr(model, "device", None)
    if device is not None:
        return torch.device(device)
    parameter = next(model.parameters())
    return parameter.device


def _score_batch(
    model: Any,
    tokenizer: Any,
    records: Sequence[TokenizedContinuation],
) -> list[float]:
    pad_id = tokenizer.pad_token_id
    if pad_id is None:
        raise ValueError("tokenizer must define pad_token_id")
    width = max(len(record.token_ids) for record in records)
    input_rows = []
    mask_rows = []
    for record in records:
        padding = width - len(record.token_ids)
        input_rows.append([pad_id] * padding + list(record.token_ids))
        mask_rows.append([0] * padding + [1] * len(record.token_ids))
    device = _model_device(model)
    input_ids = torch.tensor(input_rows, dtype=torch.long, device=device)
    attention_mask = torch.tensor(mask_rows, dtype=torch.long, device=device)
    with torch.inference_mode():
        logits = model(input_ids=input_ids, attention_mask=attention_mask).logits
    if not torch.isfinite(logits).all():
        raise TrajectoryNonFiniteError(
            "continuation-score", "continuation logits contain non-finite values"
        )
    logprobs = torch.log_softmax(logits.to(torch.float32), dim=-1)
    scores = []
    for row, record in enumerate(records):
        padding = width - len(record.token_ids)
        token_positions = torch.arange(
            padding + record.suffix_start,
            width,
            device=logprobs.device,
        )
        prediction_positions = token_positions - 1
        if bool((prediction_positions < padding).any()):
            raise ValueError("suffix has no causal prediction position")
        targets = torch.tensor(
            record.token_ids[record.suffix_start :],
            dtype=torch.long,
            device=logprobs.device,
        )
        values = logprobs[row, prediction_positions, targets]
        mean = values.mean()
        if not torch.isfinite(mean):
            raise TrajectoryNonFiniteError(
                "continuation-score", "continuation log probability is non-finite"
            )
        scores.append(float(mean.cpu()))
    return scores


def continuation_logprobs(
    model: Any,
    tokenizer: Any,
    prompts: Sequence[str],
    continuations: Sequence[str],
    batch_tokens: int,
) -> Tensor:
    """Return length-normalized log probabilities with shape ``[P, C]``.

    Args:
        model: A causal language model returning an object with ``logits``.
        tokenizer: Matching tokenizer configured with a padding token.
        prompts: Fully rendered, unpadded prompt strings.
        continuations: Short response prefixes.
        batch_tokens: Hard padded-token budget for every forward batch.

    Returns:
        CPU FP32 scores ordered by prompt then continuation.
    """
    if batch_tokens <= 0:
        raise ValueError("batch_tokens must be positive")
    records = tokenize_continuations(tokenizer, prompts, continuations)
    values = []
    for batch in _batches(records, batch_tokens):
        values.extend(_score_batch(model, tokenizer, batch))
    result = torch.tensor(values, dtype=torch.float32).reshape(
        len(prompts), len(continuations)
    )
    if not torch.isfinite(result).all():
        raise TrajectoryNonFiniteError(
            "continuation-score", "continuation scores contain non-finite values"
        )
    return result


def logmeanexp(values: Tensor, dim: int) -> Tensor:
    """Compute ``log(mean(exp(values)))`` without avoidable overflow."""
    if values.shape[dim] == 0:
        raise ValueError("logmeanexp requires a non-empty dimension")
    result = torch.logsumexp(values.to(torch.float32), dim=dim)
    return result - math.log(values.shape[dim])


def validate_prefix_groups(
    tokenizer: Any,
    refusal_prefixes: Sequence[str],
    answer_prefixes: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Normalize prefix groups and reject text/token identity collisions."""
    groups = []
    token_groups = []
    for raw_group in (refusal_prefixes, answer_prefixes):
        normalized = tuple(prefix.strip() for prefix in raw_group)
        if not normalized or any(not prefix for prefix in normalized):
            raise ValueError("prefix groups must be non-empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("prefixes must be unique after stripping")
        tokens = tuple(_token_ids(tokenizer, prefix) for prefix in normalized)
        special_ids = set(getattr(tokenizer, "all_special_ids", ()))
        if any(not value for value in tokens) or any(
            not any(token not in special_ids for token in value) for value in tokens
        ):
            raise ValueError("each prefix must contain a non-special token")
        if len(tokens) != len(set(tokens)):
            raise ValueError("prefix token sequences must be unique")
        groups.append(normalized)
        token_groups.append(tokens)
    if set(token_groups[0]) & set(token_groups[1]):
        raise ValueError("refusal and answer prefix tokens must be disjoint")
    return groups[0], groups[1]
