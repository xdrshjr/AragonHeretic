# SPDX-License-Identifier: AGPL-3.0-or-later
"""用真实 PEFT 小模型验证选中因子的导出、重载及原模型状态恢复。"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import torch

from heretic.ara import ModuleKey, TargetModule
from heretic.ara_refinement_capture import snapshot_factors, tensor_identity
from heretic.ara_research_schema import file_digest
from heretic.pro6000_experiment import _export_adapter


def tiny_peft_model():
    from peft import LoraConfig, get_peft_model
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from transformers import (
        LlamaConfig,
        LlamaForCausalLM,
        PreTrainedTokenizerFast,
    )

    config = LlamaConfig(
        hidden_size=16,
        intermediate_size=32,
        num_hidden_layers=1,
        num_attention_heads=2,
        num_key_value_heads=2,
        vocab_size=32,
    )
    model = get_peft_model(
        LlamaForCausalLM(config),
        LoraConfig(
            r=2,
            lora_alpha=2,
            target_modules=["o_proj"],
            task_type="CAUSAL_LM",
        ),
    )
    name, module = next(
        (name, module)
        for name, module in model.named_modules()
        if hasattr(module, "lora_A")
    )
    target = TargetModule(ModuleKey(0, "attn.o_proj", 0), name, module, 16, 16)
    tokenizer = PreTrainedTokenizerFast(
        tokenizer_object=Tokenizer(
            WordLevel({"<unk>": 0, "hello": 1}, unk_token="<unk>"),
        ),
        unk_token="<unk>",
    )
    return SimpleNamespace(
        model=model, tokenizer=tokenizer, ara_targets=(target,)
    )


class ExportTests(unittest.TestCase):
    def test_exported_adapter_reloads_selected_factors_and_restores_base(self):
        from peft import PeftModel
        from transformers import AutoTokenizer, LlamaForCausalLM

        model = tiny_peft_model()
        original = tensor_identity(snapshot_factors(model.ara_targets))
        selected = snapshot_factors(model.ara_targets)
        next(
            value for key, value in selected.items() if key.endswith(".B")
        ).fill_(0.125)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "factors.pt"
            torch.save(selected, snapshot)
            row = {
                "final_snapshot_path": str(snapshot),
                "snapshot_file_hash": file_digest(snapshot),
                "final_snapshot_hash": tensor_identity(selected),
            }
            export = _export_adapter({"model": model}, row, root)
            restored = tensor_identity(snapshot_factors(model.ara_targets))
            self.assertEqual(restored, original)
            reloaded = PeftModel.from_pretrained(
                LlamaForCausalLM(model.model.config),
                export["path"],
                local_files_only=True,
            )
            factor = next(
                value
                for name, value in reloaded.named_parameters()
                if ".lora_B." in name
            )
            self.assertTrue(torch.equal(factor, torch.full_like(factor, 0.125)))
            tokenizer = AutoTokenizer.from_pretrained(
                export["path"], local_files_only=True
            )
            self.assertEqual(tokenizer.unk_token, "<unk>")
            for name, expected in export["file_hashes"].items():
                self.assertEqual(
                    file_digest(Path(export["path"]) / name), expected
                )
            snapshot.write_bytes(b"corrupted snapshot")
            with self.assertRaisesRegex(ValueError, "损坏"):
                _export_adapter({"model": model}, row, root)
            self.assertEqual(
                tensor_identity(snapshot_factors(model.ara_targets)), original
            )


if __name__ == "__main__":
    unittest.main()
