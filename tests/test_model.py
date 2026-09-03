# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026  Philipp Emanuel Weidmann <pew@worldwidemann.com> + contributors

import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from torch import nn
from transformers import (
    AutoModelForImageTextToText,
    PreTrainedModel,
    PreTrainedTokenizerBase,
)

from heretic.ara import ModuleKey, TargetModule
from heretic.config import AbliterationMethod, Settings
from heretic.model import (
    Model,
    _manifest_fingerprint,
    discover_layer_modules,
    get_model_class,
    merge_generation_kwargs,
)


class FakeQwenLayer(nn.Module):
    def __init__(self, linear_attention: bool):
        super().__init__()
        attention = nn.Module()
        attention.add_module(
            "out_proj" if linear_attention else "o_proj", nn.Linear(4, 4)
        )
        self.add_module("linear_attn" if linear_attention else "self_attn", attention)
        self.mlp = nn.Module()
        self.mlp.down_proj = nn.Linear(4, 4)


class ModelFacadeTests(unittest.TestCase):
    def test_generation_precedence_preserves_greedy_default(self) -> None:
        self.assertEqual(
            merge_generation_kwargs({}, {}, {"pad_token_id": 7}),
            {"do_sample": False, "pad_token_id": 7},
        )
        merged = merge_generation_kwargs(
            {"max_new_tokens": 99, "do_sample": True},
            {"max_new_tokens": 1, "use_cache": False},
            {"pad_token_id": 3},
        )
        self.assertEqual(merged["max_new_tokens"], 1)
        self.assertFalse(merged["use_cache"])
        self.assertEqual(merged["pad_token_id"], 3)

    def test_qwen_hybrid_graph_discovers_expected_inventory(self) -> None:
        layers = [FakeQwenLayer(index >= 16) for index in range(64)]
        attention_names = []
        mlp_count = 0
        for layer in layers:
            modules = discover_layer_modules(layer, ("attn.o_proj", "mlp.down_proj"))
            attention_names.append(
                "linear" if hasattr(layer, "linear_attn") else "self"
            )
            self.assertEqual(len(modules["attn.o_proj"]), 1)
            mlp_count += len(modules["mlp.down_proj"])
        self.assertEqual(attention_names.count("self"), 16)
        self.assertEqual(attention_names.count("linear"), 48)
        self.assertEqual(mlp_count, 64)

    def test_qwen_runtime_rejects_wrong_projection_shape(self) -> None:
        model = Model.__new__(Model)
        model.settings = cast(
            Settings,
            SimpleNamespace(
                abliteration_method=AbliterationMethod.ARA,
                model="Qwen/Qwen3.8-27B",
            ),
        )
        model.ara_targets = (
            TargetModule(
                ModuleKey(0, "attn.o_proj", 0),
                "model.layers.0.self_attn.o_proj",
                nn.Linear(1, 1),
                1,
                1,
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "invalid_shapes"):
            model._validate_ara_runtime()

    def test_fused_expert_container_is_not_assumed_iterable(self) -> None:
        layer = FakeQwenLayer(False)
        delattr(layer.mlp, "down_proj")
        layer.mlp.experts = nn.Module()
        modules = discover_layer_modules(layer, ("attn.o_proj", "mlp.down_proj"))
        self.assertEqual(len(modules["attn.o_proj"]), 1)
        self.assertNotIn("mlp.down_proj", modules)

    def test_model_class_lookup_forwards_revision(self) -> None:
        with patch(
            "heretic.model.PretrainedConfig.get_config_dict",
            return_value=({"vision_config": {}}, {}),
        ) as lookup:
            self.assertIs(
                get_model_class("vendor/model", revision="fixed"),
                AutoModelForImageTextToText,
            )
        lookup.assert_called_once_with("vendor/model", revision="fixed")

    def test_chat_template_settings_are_shared_by_batch_and_stream_paths(self) -> None:
        model = Model.__new__(Model)
        model.settings = cast(
            Settings,
            SimpleNamespace(chat_template_kwargs={"enable_thinking": False}),
        )
        tokenizer = SimpleNamespace()
        calls = []

        def render(chat, **kwargs):
            calls.append((chat, kwargs))
            return "answer" if isinstance(chat[0], dict) else ["answer"]

        tokenizer.apply_chat_template = render
        model.tokenizer = cast(PreTrainedTokenizerBase, tokenizer)
        batch = [[{"role": "user", "content": "batch"}]]
        stream = [{"role": "user", "content": "stream"}]
        self.assertEqual(model._render_chat_template(batch), ["answer"])
        self.assertEqual(model._render_chat_template(stream), "answer")
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertFalse(calls[0][1]["enable_thinking"])
        tokenizer.apply_chat_template = lambda *_args, **_kwargs: "<think>open"
        with self.assertRaisesRegex(RuntimeError, "unclosed"):
            model._render_chat_template(stream)

    def test_merge_reload_requires_original_base_fingerprint(self) -> None:
        class FakeBase(nn.Module):
            def __init__(self):
                super().__init__()
                self.model = nn.Module()
                self.model.layers = nn.ModuleList([nn.Module()])
                self.model.layers[0].proj = nn.Linear(4, 3, bias=False)
                self.config = SimpleNamespace(_commit_hash="commit", model_type="fake")

        model = Model.__new__(Model)
        model.model_manifest = {
            "model": "org/model",
            "requested_revision": "commit",
            "commit": "commit",
            "model_type": "fake",
            "layers": 1,
            "targets": [{"name": "model.layers.0.proj", "shape": [3, 4]}],
            "chat_template_sha256": "template",
        }
        model.model_fingerprint = _manifest_fingerprint(model.model_manifest)
        base = cast(PreTrainedModel, FakeBase())
        model._verify_reloaded_base(base)
        base.config._commit_hash = "different"  # ty:ignore[unresolved-attribute]
        with self.assertRaisesRegex(RuntimeError, "fingerprint"):
            model._verify_reloaded_base(base)


if __name__ == "__main__":
    unittest.main()
