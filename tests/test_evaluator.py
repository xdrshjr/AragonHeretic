# SPDX-License-Identifier: AGPL-3.0-or-later

import unittest
from types import SimpleNamespace

from heretic.evaluator import Evaluator, ScorerEntry
from heretic.plugin import Context
from heretic.scorer import Score
from heretic.utils import Prompt


class RecordingScorer:
    def __init__(self):
        self.contexts = []

    def get_score(self, context):
        self.contexts.append(context)
        return Score(0.0, "0", "0")


class EvaluatorTests(unittest.TestCase):
    def test_one_score_pass_shares_context_but_next_pass_does_not(self) -> None:
        evaluator = Evaluator.__new__(Evaluator)
        evaluator.settings = SimpleNamespace()
        evaluator.model = SimpleNamespace()
        first = RecordingScorer()
        second = RecordingScorer()
        config = SimpleNamespace(optimization="none")
        evaluator._scorer_entries = [
            ScorerEntry(first, "first", config),
            ScorerEntry(second, "second", config),
        ]
        evaluator.get_scores()
        evaluator.get_scores()
        self.assertIs(first.contexts[0], second.contexts[0])
        self.assertIsNot(first.contexts[0], first.contexts[1])

    def test_paired_records_omit_absent_machine_evidence(self) -> None:
        evaluator = Evaluator.__new__(Evaluator)
        evaluator._scorer_entries = []
        evaluator.baseline_scores = [("score", Score(0.0, "0", "0"))]
        records = evaluator.get_paired_score_records(
            [("score", Score(0.1, "0.1", "0.1"))]
        )
        self.assertNotIn("sample_count", records[0]["score"])
        self.assertNotIn("dataset_fingerprint", records[0]["baseline"])

    def test_paired_records_reject_machine_identity_drift(self) -> None:
        evaluator = Evaluator.__new__(Evaluator)
        evaluator._scorer_entries = []
        evaluator.baseline_scores = [("score", Score(0.0, "0", "0", 100, "baseline"))]
        with self.assertRaisesRegex(ValueError, "dataset_fingerprint"):
            evaluator.get_paired_score_records(
                [("score", Score(0.1, "0.1", "0.1", 100, "changed"))]
            )

    def test_continuation_cache_lives_for_exactly_one_context(self) -> None:
        class FakeModel:
            calls = 0

            def continuation_cache_identity(self, continuations):
                return ((tuple(range(len(continuations))),), "tokenizer")

            def get_continuation_logprobs(self, prompts, continuations, budget):
                del prompts, continuations, budget
                self.calls += 1
                return self.calls

        model = FakeModel()
        prompts = [Prompt("system", "user")]
        first = Context(settings=SimpleNamespace(), model=model)
        self.assertEqual(first.get_continuation_logprobs(prompts, ("a",), 8), 1)
        self.assertEqual(first.get_continuation_logprobs(prompts, ("a",), 8), 1)
        second = Context(settings=SimpleNamespace(), model=model)
        self.assertEqual(second.get_continuation_logprobs(prompts, ("a",), 8), 2)


if __name__ == "__main__":
    unittest.main()
