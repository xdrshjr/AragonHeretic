# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import tempfile
import unittest
from pathlib import Path

from heretic.engineering_eval import (
    attach_prompt_evidence,
    build_parser,
    load_exported_adapter,
    prepare_options,
    resolve_data_range,
    summarize_scores,
    validate_adapter_config,
)


class EngineeringEvalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.adapter = self.root / "artifacts" / "best-adapter"
        self.adapter.mkdir(parents=True)
        self.config = {"peft_type": "LORA", "r": 2, "lora_alpha": 2}
        self.write_config()
        (self.adapter / "adapter_model.safetensors").touch()
        (self.root / "engineering-config.json").write_text(json.dumps({
            "per_side_data": {
                "fit": {"stop": 8}, "monitor": {"stop": 16},
                "development": {"stop": 36},
            },
        }))

    def write_config(self):
        (self.adapter / "adapter_config.json").write_text(
            json.dumps(self.config)
        )

    def test_auto_evaluation_excludes_entire_training_run(self):
        rows = resolve_data_range(self.adapter, "auto", 100)
        self.assertEqual((rows["start"], rows["stop"]), (36, 136))
        self.assertFalse(rows["overlaps_training_run"])

    def test_explicit_development_replay_records_overlap(self):
        rows = resolve_data_range(self.adapter, "16", 20)
        self.assertTrue(rows["overlaps_training_run"])
        self.assertEqual(rows["stop"], 36)

    def test_range_rejects_invalid_or_excess_samples(self):
        for start, count in [("-1", 20), ("auto", 365), ("0", 0)]:
            with self.subTest(start=start, count=count):
                with self.assertRaises(ValueError):
                    resolve_data_range(self.adapter, start, count)

    def test_moved_adapter_requires_explicit_start(self):
        moved = self.root / "moved"
        with self.assertRaisesRegex(ValueError, "--start-index"):
            resolve_data_range(moved, "auto", 20)
        rows = resolve_data_range(moved, "0", 20)
        self.assertIsNone(rows["overlaps_training_run"])

    def test_non_unit_scaling_is_rejected(self):
        self.config["lora_alpha"] = 4
        self.write_config()
        with self.assertRaisesRegex(ValueError, "unit scaling"):
            validate_adapter_config(self.adapter)

    def test_summary_does_not_claim_improvement_for_equal_scores(self):
        scores = {
            "baseline_keywords": 0.0, "keywords": 0.0,
            "first_token_kl": 0.2, "sequence_kl": 0.01, "sample_count": 20,
        }
        result = summarize_scores(scores)
        self.assertEqual(result["effect_status"], "unchanged")
        self.assertIsNone(result["relative_reduction"])
        self.assertFalse(result["within_kl_reference"])
        scores.update(baseline_keywords=0.95, keywords=0.65)
        result = summarize_scores(scores)
        self.assertAlmostEqual(result["reduction_percentage_points"], 30)
        self.assertEqual(result["effect_status"], "improved")

    def prompt_fixture(self):
        from types import SimpleNamespace

        from heretic.ara_research_schema import digest

        prompts, roles = {}, {}
        for side in ("good", "bad"):
            prompts[side] = [
                SimpleNamespace(system="System", user=f"{side} question {n}")
                for n in range(2)
            ]
            roles[f"evaluation.{side}"] = {"prompts": [
                {"prompt_id": f"{side}-{n}",
                 "normalized_text_hash": digest([p.system, p.user])}
                for n, p in enumerate(prompts[side])
            ]}
        scores = {key: [
            {"prompt_id": f"bad-{n}", "text": f"{key} answer {n}"}
            for n in (1, 0)
        ] for key in ("responses", "baseline_responses")}
        return scores, {"roles": roles}, prompts

    def test_evidence_matches_questions_by_id_and_preserves_answers(self):
        scores, protocol, prompts = self.prompt_fixture()
        result = attach_prompt_evidence(scores, protocol, prompts)
        self.assertEqual(len(result["inputs"]["good"]), 2)
        for key in ("responses", "baseline_responses"):
            self.assertEqual(result[key][0]["question"], "bad question 1")
            self.assertEqual(result[key][0]["system_prompt"], "System")
            self.assertEqual(result[key][0]["text"], scores[key][0]["text"])
            self.assertNotIn("question", scores[key][0])

    def test_evidence_rejects_changed_source_questions(self):
        scores, protocol, prompts = self.prompt_fixture()
        prompts["bad"][0].user = "Different question"
        with self.assertRaisesRegex(ValueError, "Prompt content mismatch"):
            attach_prompt_evidence(scores, protocol, prompts)

    def test_evidence_rejects_unmatched_response_ids(self):
        scores, protocol, prompts = self.prompt_fixture()
        scores["responses"][0]["prompt_id"] = "unknown"
        with self.assertRaisesRegex(ValueError, "IDs do not match"):
            attach_prompt_evidence(scores, protocol, prompts)

    def test_dry_run_preparation_does_not_write_outputs(self):
        model = self.root / "model"
        model.mkdir()
        (model / "config.json").write_text("{}")
        template = self.root / "config.toml"
        template.touch()
        output = self.root / "output"
        options = build_parser().parse_args([
            "--model", str(model), "--adapter", str(self.adapter),
            "--config-template", str(template), "--output-dir", str(output),
            "--dry-run",
        ])
        prepare_options(options)
        self.assertFalse(output.exists())
        output.mkdir()
        (output / "existing-result.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "not empty"):
            prepare_options(options)

    def test_export_load_preserves_candidate_and_disabled_baseline(self):
        import torch
        from peft import LoraConfig, get_peft_model
        from transformers import LlamaConfig, LlamaForCausalLM

        from heretic.ara_refinement_capture import disabled_targets
        from heretic.ara import TargetModule, ModuleKey
        from types import SimpleNamespace

        torch.manual_seed(42)
        base = LlamaForCausalLM(LlamaConfig(
            vocab_size=16, hidden_size=8, intermediate_size=16,
            num_hidden_layers=1, num_attention_heads=2,
            num_key_value_heads=2,
        ))
        network = get_peft_model(base, LoraConfig(
            r=2, lora_alpha=2, target_modules=["o_proj"],
            task_type="CAUSAL_LM",
        ))
        network.eval()
        layer = network.base_model.model.model.layers[0].self_attn.o_proj
        tokens = torch.tensor([[1, 2, 3]])
        with torch.no_grad():
            baseline = network(tokens).logits.clone()
            layer.lora_B["default"].weight.fill_(0.3)
            candidate = network(tokens).logits.clone()
            network.save_pretrained(self.adapter)
            layer.lora_B["default"].weight.zero_()
        self.assertFalse(torch.allclose(baseline, candidate))
        load_exported_adapter(SimpleNamespace(model=network), self.adapter)
        target = TargetModule(ModuleKey(0, "attn.o_proj", 0), "o_proj",
                              layer, 8, 8)
        with torch.no_grad():
            torch.testing.assert_close(network(tokens).logits, candidate)
            with disabled_targets([target]):
                torch.testing.assert_close(network(tokens).logits, baseline)
            torch.testing.assert_close(network(tokens).logits, candidate)


if __name__ == "__main__":
    unittest.main()
