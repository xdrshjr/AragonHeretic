# SPDX-License-Identifier: AGPL-3.0-or-later

import unittest
import torch

from heretic.sequence_scores import masked_token_kl, logits_on_sequence
from heretic.ara_refinement_capture import TokenSequence
from test_ara_refinement_capture import tiny_model


class SequenceKLTests(unittest.TestCase):
    def test_same_model_has_zero_kl(self):
        logits = torch.randn(7, 11)
        values = masked_token_kl(
            logits, logits, torch.ones(7, dtype=torch.bool), 2
        )
        self.assertTrue(torch.equal(values, torch.zeros(7)))

    def test_kl_direction_matches_manual_distribution(self):
        base = torch.tensor([[0.8, 0.2], [0.1, 0.9]])
        candidate = torch.tensor([[0.5, 0.5], [0.6, 0.4]])
        actual = masked_token_kl(
            base.log(), candidate.log(), torch.tensor([True, False])
        )
        expected = (base[0] * (base[0].log() - candidate[0].log())).sum()
        self.assertAlmostEqual(float(actual[0]), float(expected), places=6)
        self.assertEqual(len(actual), 1)

    def test_zero_effective_positions_fail(self):
        with self.assertRaises(ValueError):
            masked_token_kl(
                torch.zeros(2, 3),
                torch.zeros(2, 3),
                torch.zeros(2, dtype=torch.bool),
            )

    def test_immediate_eos_still_has_one_prediction(self):
        model = tiny_model()
        logits = logits_on_sequence(model, TokenSequence("eos", (1, 4), (2,)))
        self.assertEqual(logits.shape, (1, 4))


if __name__ == "__main__":
    unittest.main()
