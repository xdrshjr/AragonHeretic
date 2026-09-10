# SPDX-License-Identifier: AGPL-3.0-or-later
"""两层小模型验证顺序条件输入、绝对替换和异常恢复。"""

import unittest
from types import SimpleNamespace

import torch
from torch import nn

from heretic.ara import ModuleKey, TargetModule
from heretic.ara_refinement_capture import (
    CaptureRequest,
    TokenSequence,
    apply_snapshot,
    bind_evaluation_state,
    capture_reference_pair,
    causal_positions,
    disabled_targets,
    snapshot_factors,
    tensor_identity,
)


class TinyProjection(nn.Module):
    def __init__(self):
        super().__init__()
        self.base_layer = nn.Linear(4, 4, bias=False)
        self.base_layer.weight.data.copy_(torch.eye(4))
        self.lora_A = nn.ModuleDict({"default": nn.Linear(4, 2, bias=False)})
        self.lora_B = nn.ModuleDict({"default": nn.Linear(2, 4, bias=False)})
        self.lora_A["default"].weight.data.copy_(torch.eye(4)[:2])
        self.lora_B["default"].weight.data.zero_()
        self.active_adapters = ["default"]
        self.scaling = {"default": 1}
        self.use_dora = {"default": False}
        self._disable_adapters = False

    def forward(self, value):
        result = self.base_layer(value)
        if not self._disable_adapters:
            result = result + self.lora_B["default"](
                self.lora_A["default"](value)
            )
        return result


class TinyNetwork(nn.Module):
    def __init__(self):
        super().__init__()
        self.embedding = nn.Embedding(16, 4)
        self.layers = nn.ModuleList([TinyProjection(), TinyProjection()])
        self.config = SimpleNamespace(max_position_embeddings=64)

    def get_input_embeddings(self):
        return self.embedding

    def forward(self, input_ids, attention_mask=None, use_cache=False):
        value = self.embedding(input_ids)
        for layer in self.layers:
            value = layer(value)
        return SimpleNamespace(logits=value)


def tiny_model():
    torch.manual_seed(42)
    model = TinyNetwork()
    targets = tuple(
        TargetModule(ModuleKey(i, "attn.o_proj", 0), f"layers.{i}", layer, 4, 4)
        for i, layer in enumerate(model.layers)
    )
    return SimpleNamespace(model=model, ara_targets=targets)


def sequences():
    return {
        side: (
            TokenSequence(f"{side}-0", (1, 4), (5, 6)),
            TokenSequence(f"{side}-1", (7, 8), (9,)),
        )
        for side in ("good", "bad")
    }


class CaptureTests(unittest.TestCase):
    def test_restore_failure_still_restores_adapter_flags(self):
        from unittest.mock import patch

        model = tiny_model()
        with patch(
            "heretic.ara_refinement_capture.apply_snapshot",
            side_effect=RuntimeError("复制失败"),
        ):
            with self.assertRaisesRegex(RuntimeError, "复制失败"):
                with disabled_targets(model.ara_targets):
                    model.model.layers[0]._disable_adapters = True
        self.assertFalse(model.model.layers[0]._disable_adapters)

    def test_snapshot_restoration_does_not_allocate_device_copy(self):
        from unittest.mock import patch

        model = tiny_model()
        state = snapshot_factors(model.ara_targets)
        with patch.object(torch.Tensor, "to", side_effect=RuntimeError):
            apply_snapshot(model.ara_targets, state)
        self.assertEqual(
            snapshot_factors(model.ara_targets).keys(), state.keys()
        )

    def test_left_and_right_padding_use_same_unpadded_positions(self):
        masks = torch.tensor([[0, 0, 1, 1, 1], [1, 1, 1, 0, 0]])
        positions = causal_positions(masks, [2, 2], [2, 2])
        self.assertEqual(positions.tolist(), [[0, 3], [0, 4], [1, 1], [1, 2]])

    def test_recapture_matches_true_linear_downstream_input(self):
        model = tiny_model()
        target = model.ara_targets[1:]
        request = CaptureRequest(target, sequences(), "protocol", "base")
        frozen = capture_reference_pair(model, request)
        model.model.layers[0].lora_B["default"].weight.data.fill_(0.2)
        current = capture_reference_pair(model, request)
        key = target[0].key
        self.assertFalse(
            torch.allclose(
                frozen.pairs[key].good.inputs, current.pairs[key].good.inputs
            )
        )
        self.assertTrue(
            torch.allclose(
                current.pairs[key].good.inputs, current.pairs[key].good.minus
            )
        )
        self.assertTrue(
            torch.allclose(
                frozen.pairs[key].good.reference,
                current.pairs[key].good.reference,
            )
        )

    def test_short_steps_drop_on_both_sides_and_renormalize(self):
        model = tiny_model()
        bank = capture_reference_pair(
            model, CaptureRequest(model.ara_targets, sequences(), "p", "b")
        )
        for pair in bank.pairs.values():
            self.assertEqual(list(pair.scales), [0])
            self.assertTrue(torch.equal(pair.good.weights, torch.ones(2)))

    def test_context_restores_all_factors_and_flags_on_exception(self):
        model = tiny_model()
        state = snapshot_factors(model.ara_targets)
        identity = tensor_identity(state)
        with self.assertRaises(RuntimeError):
            with disabled_targets(model.ara_targets):
                model.model.layers[0].lora_A["default"].weight.data.fill_(9.0)
                model.model.layers[0]._disable_adapters = True
                raise RuntimeError("故障注入")
        self.assertEqual(
            tensor_identity(snapshot_factors(model.ara_targets)), identity
        )
        self.assertFalse(model.model.layers[0]._disable_adapters)

    def test_absolute_snapshot_does_not_accumulate(self):
        model = tiny_model()
        state = snapshot_factors(model.ara_targets)
        state["layers.0.B"].fill_(0.1)
        apply_snapshot(model.ara_targets, state)
        first = tensor_identity(snapshot_factors(model.ara_targets))
        apply_snapshot(model.ara_targets, state)
        self.assertEqual(
            first, tensor_identity(snapshot_factors(model.ara_targets))
        )

    def test_sequential_cache_rejects_other_states(self):
        model = tiny_model()
        request = CaptureRequest(model.ara_targets, sequences(), "p", "b")
        bank = capture_reference_pair(model, request)
        with self.assertRaises(ValueError):
            bind_evaluation_state(bank, "changed")
        request = CaptureRequest(
            model.ara_targets, sequences(), "p", "b", "frozen-diagnostic"
        )
        bank = capture_reference_pair(model, request)
        diagnostic = bind_evaluation_state(bank, "changed")
        self.assertNotEqual(
            diagnostic.manifest["captured_state_hash"],
            diagnostic.manifest["evaluation_state_hash"],
        )


if __name__ == "__main__":
    unittest.main()
