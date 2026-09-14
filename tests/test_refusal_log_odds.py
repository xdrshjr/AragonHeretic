# SPDX-License-Identifier: AGPL-3.0-or-later

import unittest
from types import SimpleNamespace

import torch
from torch import nn

from heretic.continuation_scores import (
    continuation_logprobs,
    logmeanexp,
    tokenize_continuations,
    validate_prefix_groups,
)


class CharacterTokenizer:
    pad_token_id = 0
    all_special_ids = [0]

    def __call__(self, text: str, **kwargs):
        del kwargs
        return {"input_ids": [ord(character) for character in text]}


class NextTokenModel(nn.Module):
    def __init__(self, preferred: int):
        super().__init__()
        self.anchor = nn.Parameter(torch.zeros(()))
        self.preferred = preferred

    def forward(self, input_ids, attention_mask):
        del attention_mask
        batch, width = input_ids.shape
        logits = torch.zeros((batch, width, 256), device=input_ids.device)
        next_tokens = input_ids[:, 1:]
        logits[:, :-1].scatter_(2, next_tokens.unsqueeze(-1), 2.0)
        logits[:, :, self.preferred] += 3.0
        return SimpleNamespace(logits=logits)


class ContinuationScoreTests(unittest.TestCase):
    def test_causal_shift_and_length_normalization(self) -> None:
        tokenizer = CharacterTokenizer()
        values = continuation_logprobs(
            NextTokenModel(ord("r")),
            tokenizer,
            ["p", "long"],
            ["r", "aa"],
            32,
        )
        self.assertEqual(tuple(values.shape), (2, 2))
        self.assertTrue(torch.isfinite(values).all())
        self.assertTrue(torch.all(values[:, 0] > values[:, 1]))
        single = continuation_logprobs(
            NextTokenModel(ord("r")), tokenizer, ["p"], ["r", "aa"], 32
        )
        self.assertTrue(torch.allclose(values[:1], single))

    def test_raw_log_ratio_is_strictly_monotonic(self) -> None:
        tokenizer = CharacterTokenizer()
        low = continuation_logprobs(
            NextTokenModel(ord("a")), tokenizer, ["p"], ["r", "a"], 8
        )
        high = continuation_logprobs(
            NextTokenModel(ord("r")), tokenizer, ["p"], ["r", "a"], 8
        )
        low_ratio = logmeanexp(low[:, :1], 1) - logmeanexp(low[:, 1:], 1)
        high_ratio = logmeanexp(high[:, :1], 1) - logmeanexp(high[:, 1:], 1)
        self.assertGreater(float(high_ratio), float(low_ratio))

    def test_invalid_prefixes_and_changed_boundaries_are_rejected(self) -> None:
        tokenizer = CharacterTokenizer()
        with self.assertRaises(ValueError):
            validate_prefix_groups(tokenizer, ["r", "r"], ["a"])

        class BoundaryTokenizer(CharacterTokenizer):
            def __call__(self, text: str, **kwargs):
                if text == "px":
                    return {"input_ids": [999]}
                return super().__call__(text, **kwargs)

        with self.assertRaisesRegex(ValueError, "boundary"):
            tokenize_continuations(BoundaryTokenizer(), ["p"], ["x"])

    def test_multitoken_chinese_prefix_groups_are_distinct(self) -> None:
        refusal, answer = validate_prefix_groups(
            CharacterTokenizer(),
            ["抱歉", "我不能"],
            ["可以", "以下是"],
        )
        self.assertEqual(refusal, ("抱歉", "我不能"))
        self.assertEqual(answer, ("可以", "以下是"))


class StreamingPrefixTests(unittest.TestCase):
    def test_long_prompt_normalizes_only_selected_full_vocab_rows(self):
        from unittest.mock import patch
        from heretic.continuation_scores import _selected_logprobs

        logits = torch.randn(1024, 257, dtype=torch.bfloat16)
        positions = torch.arange(1000, 1024)
        targets = torch.arange(24)
        expected = logits.float().log_softmax(-1)[positions, targets]
        original = torch.log_softmax
        shapes = []

        def measured(values, dim):
            shapes.append(tuple(values.shape))
            return original(values, dim=dim)

        with patch("torch.log_softmax", side_effect=measured):
            actual = _selected_logprobs(logits, positions, targets)
        self.assertTrue(torch.equal(actual, expected))
        self.assertEqual(shapes, [(8, 257)] * 3)


if __name__ == "__main__":
    unittest.main()
