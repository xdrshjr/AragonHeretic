# SPDX-License-Identifier: AGPL-3.0-or-later
"""语义分母、冻结标签和能力统计的行为回归。"""

import unittest

from heretic.research_evaluation import (
    clopper_pearson_upper,
    paired_ability_bootstrap,
    response_hash,
    summarize_semantics,
)


def semantic_fixture(count=1):
    prompts, responses, labels = [], [], []
    for index in range(count):
        key = str(index)
        prompts.append(
            {
                "prompt_id": key,
                "scenario_group_id": key,
                "primary_in_group": True,
            }
        )
        responses.append(
            {
                "prompt_id": key,
                "text": "完整回答",
                "response_hash": response_hash("完整回答"),
                "effective_length": 3,
                "finish_reason": "stop",
            }
        )
        labels.append(
            {
                **prompts[-1],
                "response_hash": responses[-1]["response_hash"],
                "anonymous_method_id": "anonymous",
                "rubric_hash": "rubric",
                "effective_length": 3,
                "finish_reason": "stop",
                "reviews": [
                    {"reviewer_id": name, "refusal": "none", "answer": "valid"}
                    for name in ("one", "two")
                ],
            }
        )
    return (
        prompts,
        responses,
        labels,
        {"anonymous_method_id": "anonymous", "rubric_hash": "rubric"},
    )


class SemanticEvaluationTests(unittest.TestCase):
    def test_exact_zero_refusal_bounds(self):
        self.assertAlmostEqual(clopper_pearson_upper(0, 100), 0.0295130496)
        self.assertAlmostEqual(clopper_pearson_upper(0, 300), 0.00993608194)
        self.assertEqual(clopper_pearson_upper(4, 4), 1.0)
        self.assertAlmostEqual(clopper_pearson_upper(1, 2), 0.9746794345)

    def test_invalid_binomial_denominators_fail(self):
        for pair in ((0, 0), (2, 1), (-1, 10), (True, 2)):
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                clopper_pearson_upper(*pair)

    def test_missing_labels_never_shrink_denominator(self):
        prompts, responses, labels, identity = semantic_fixture(3)
        result = summarize_semantics(prompts, responses, labels[:1], identity)
        self.assertEqual(result["n"], 3)
        self.assertEqual(result["status"], "inconclusive")
        self.assertIsNone(result["refusal_rate"])

    def test_implicit_and_uncertain_refusals_count_as_failures(self):
        prompts, responses, labels, identity = semantic_fixture(3)
        for record, label in zip(labels, ("implicit", "uncertain", "none")):
            for review in record["reviews"]:
                review["refusal"] = label
        result = summarize_semantics(prompts, responses, labels, identity)
        self.assertEqual(result["k"], 2)

    def test_empty_and_budget_truncated_answers_are_not_valid(self):
        prompts, responses, labels, identity = semantic_fixture(2)
        responses[0]["text"] = ""
        responses[0]["response_hash"] = response_hash("")
        labels[0]["response_hash"] = response_hash("")
        responses[1]["finish_reason"] = labels[1]["finish_reason"] = "length"
        result = summarize_semantics(prompts, responses, labels, identity)
        self.assertEqual(result["valid_rate"], 0.0)
        self.assertEqual(result["invalid_rate"], 1.0)

    def test_disagreement_requires_a_third_reviewer(self):
        prompts, responses, labels, identity = semantic_fixture()
        labels[0]["reviews"][0]["refusal"] = "implicit"
        result = summarize_semantics(prompts, responses, labels, identity)
        self.assertEqual(result["status"], "inconclusive")
        labels[0]["adjudication"] = {
            "reviewer_id": "three",
            "refusal": "implicit",
            "answer": "invalid",
        }
        self.assertEqual(
            summarize_semantics(prompts, responses, labels, identity)["k"], 1
        )

    def test_response_hash_and_duplicate_scenarios_fail(self):
        prompts, responses, labels, identity = semantic_fixture(2)
        responses[0]["text"] = "篡改"
        with self.assertRaises(ValueError):
            summarize_semantics(prompts, responses, labels, identity)
        prompts, responses, labels, identity = semantic_fixture(2)
        prompts[1]["scenario_group_id"] = "0"
        labels[1]["scenario_group_id"] = "0"
        with self.assertRaises(ValueError):
            summarize_semantics(prompts, responses, labels, identity)


class AbilityTests(unittest.TestCase):
    def test_paired_drop_direction_and_degenerate_intervals(self):
        tasks = {}
        for name in ("mmlu", "gsm8k", "ifeval"):
            rows = [
                {
                    "prompt_id": str(i),
                    "scenario_group_id": str(i),
                    "base": i % 2,
                    "candidate": 0,
                }
                for i in range(500)
            ]
            tasks[name] = {
                "records": rows,
                "prompt_ids": [str(i) for i in range(500)],
                "split_count": 500,
            }
        result = paired_ability_bootstrap(tasks)
        self.assertEqual(result["status"], "failed")
        self.assertGreater(result["upper"], 40.0)
        for task in tasks.values():
            for row in task["records"]:
                row["candidate"] = row["base"]
        self.assertEqual(
            paired_ability_bootstrap(tasks)["status"], "inconclusive"
        )


class SemanticSuperiorityTests(unittest.TestCase):
    def test_single_sided_interval_preserves_pairing_and_order(self):
        from heretic.research_evaluation import paired_semantic_interval

        identity = {
            "analysis": "semantic-superiority-v1",
            "seed": 20260910,
            "replicates": 10000,
        }
        records = [
            {
                "scenario_group_id": str(i),
                "primary_in_group": True,
                "candidate_refusal": 0,
                "baseline_refusal": int(i < 80),
            }
            for i in range(100)
        ]
        result = paired_semantic_interval(records, identity)
        self.assertEqual(result["status"], "passed")
        self.assertLess(result["upper"], 0)
        self.assertEqual(
            result, paired_semantic_interval(records[::-1], identity)
        )

    def test_zero_width_and_missing_members_are_inconclusive(self):
        from heretic.research_evaluation import (
            paired_semantic_interval,
            compare_semantic_members,
        )

        identity = {
            "analysis": "semantic-superiority-v1",
            "seed": 20260910,
            "replicates": 10000,
        }
        records = [
            {
                "scenario_group_id": str(i),
                "primary_in_group": True,
                "candidate_refusal": 0,
                "baseline_refusal": 1,
            }
            for i in range(10)
        ]
        self.assertEqual(
            paired_semantic_interval(records, identity)["status"],
            "inconclusive",
        )
        comparison = compare_semantic_members(
            {"semantic_comparison": {**identity, "role": "research-audit.bad"}},
            {"members": {}},
            {},
        )
        self.assertTrue(
            all(row["status"] == "inconclusive" for row in comparison.values())
        )
        with self.assertRaises(ValueError):
            paired_semantic_interval(records * 2, identity)

    def test_paired_labels_reject_wrong_response_identity(self):
        from heretic.research_evaluation import semantic_comparison_records

        prompts, responses, labels, identity = semantic_fixture(3)
        evidence = {
            "prompts": prompts,
            "responses": responses,
            "identity": identity,
        }
        records = semantic_comparison_records(evidence, labels)
        self.assertEqual(len(records), 3)
        labels[0]["response_hash"] = "wrong"
        with self.assertRaises(ValueError):
            semantic_comparison_records(evidence, labels)


if __name__ == "__main__":
    unittest.main()
