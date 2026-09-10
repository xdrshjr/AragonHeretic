# SPDX-License-Identifier: AGPL-3.0-or-later
"""基座锚定数值目标、monitor 门槛与整组回滚。"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import torch

from heretic.ara_refinement import (
    BlockProposal,
    SolverOptions,
    _block_transaction,
    monitor_accepts,
    refinement_loss,
    solve_refinement_block,
)
from heretic.ara_refinement_capture import (
    CaptureRequest,
    capture_reference_pair,
    snapshot_factors,
    tensor_identity,
)
from heretic.ara_research_runner import paired_parameters
from test_ara_refinement_capture import sequences, tiny_model


def monitor_score(keywords=0.5, odds=0.0):
    return {
        "keywords": keywords,
        "log_odds": odds,
        "first_token_kl": 0.01,
        "sequence_kl": 0.01,
        "sample_count": 64,
        "data_identity": "monitor",
    }


class RefinementTests(unittest.TestCase):
    def test_anchor_keep_accounts_for_upstream_drift(self):
        model = tiny_model()
        model.model.layers[0].lora_B["default"].weight.data.fill_(0.2)
        bank = capture_reference_pair(
            model, CaptureRequest(model.ara_targets[1:], sequences(), "p", "b")
        )
        pair = bank.pairs[model.ara_targets[1].key]
        factors = (torch.eye(4)[:2], torch.zeros(4, 2))
        anchored = refinement_loss(pair, factors, SolverOptions(0.0, 1.0, 4.0))
        local = refinement_loss(
            pair, factors, SolverOptions(0.0, 1.0, 4.0, "local-update")
        )
        self.assertGreater(float(anchored["keep"]), 0)
        self.assertEqual(float(local["keep"]), 0)

    def test_solver_operates_on_clones(self):
        model = tiny_model()
        bank = capture_reference_pair(
            model, CaptureRequest(model.ara_targets, sequences(), "p", "b")
        )
        before = tensor_identity(snapshot_factors(model.ara_targets))
        result = solve_refinement_block(
            model,
            bank,
            (model.ara_targets, paired_parameters(42, 3), "base-anchored"),
        )
        self.assertEqual(
            before, tensor_identity(snapshot_factors(model.ara_targets))
        )
        self.assertEqual(len(result.statistics), 2)

    def test_monitor_requires_nonincrease_and_real_improvement(self):
        before = monitor_score()
        self.assertFalse(monitor_accepts(before, monitor_score()))
        self.assertTrue(monitor_accepts(before, monitor_score(0.5 - 1 / 64)))
        self.assertTrue(monitor_accepts(before, monitor_score(0.5, -0.001)))
        self.assertFalse(monitor_accepts(before, monitor_score(0.6, -0.1)))
        after = monitor_score(0.4)
        after["sequence_kl"] = 0.16
        self.assertFalse(monitor_accepts(before, after))

    def test_monitor_nan_is_attempt_failure(self):
        after = monitor_score(0.4)
        after["log_odds"] = float("nan")
        with self.assertRaises(RuntimeError):
            monitor_accepts(monitor_score(), after)

    def test_block_rolls_back_all_factors_when_monitor_raises(self):
        model = tiny_model()
        original = snapshot_factors(model.ara_targets)
        candidate = {name: value.clone() for name, value in original.items()}
        candidate["layers.0.B"].fill_(0.01)
        request = CaptureRequest(model.ara_targets, sequences(), "p", "b")
        scores = iter([monitor_score(), RuntimeError("评分故障")])

        def monitor(_):
            value = next(scores)
            if isinstance(value, Exception):
                raise value
            return value

        context = SimpleNamespace(
            monitor=monitor,
            config=SimpleNamespace(keep_reference="base-anchored"),
        )
        proposal = BlockProposal(candidate, {}, True, None)
        with patch(
            "heretic.ara_refinement.solve_refinement_block",
            return_value=proposal,
        ):
            with self.assertRaises(RuntimeError):
                _block_transaction(
                    model, context, request, paired_parameters(42, 0)
                )
        self.assertEqual(
            tensor_identity(original),
            tensor_identity(snapshot_factors(model.ara_targets)),
        )


if __name__ == "__main__":
    unittest.main()
