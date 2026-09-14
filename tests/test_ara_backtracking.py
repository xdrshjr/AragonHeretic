"""整组尝试的回滚、非零判断和真实预测误差。"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import torch

from heretic.ara_refinement import BlockProposal, _block_transaction
from heretic.ara_refinement_capture import (
    CaptureRequest,
    snapshot_factors,
    tensor_identity,
)
from heretic.ara_research_runner import paired_parameters
from test_ara_refinement import monitor_score
from test_ara_refinement_capture import sequences, tiny_model


class BacktrackingTests(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(1)
        self.model = tiny_model()
        self.original = snapshot_factors(self.model.ara_targets)
        self.request = CaptureRequest(
            self.model.ara_targets, sequences(), "p", "b"
        )
        self.context = SimpleNamespace(
            config=SimpleNamespace(
                keep_reference="base-anchored",
                proposal_policy="spectral-backtrack-v1",
                backtracking_alphas=(1, 0.5, 0.25, 0.125, 0.0625),
            ),
            initial_factors=self.original,
            check_budget=lambda: {},
            monitor=lambda model: monitor_score(),
        )

    def run_transaction(self, candidate):
        proposal = BlockProposal(candidate, {}, False, "proposal_rejected")
        with patch(
            "heretic.ara_refinement.solve_refinement_block",
            return_value=proposal,
        ) as solver:
            event = _block_transaction(
                self.model, self.context, self.request, paired_parameters(42, 0)
            )
        self.assertEqual(solver.call_count, 1)
        return event

    def candidate(self):
        result = {name: value.clone() for name, value in self.original.items()}
        result["layers.0.B"].fill_(0.01)
        return result

    def test_accepts_first_monitor_improvement_and_records_discrepancy(self):
        scores = iter([monitor_score(), monitor_score(0.5, -0.01)])
        self.context.monitor = lambda model: next(scores)
        event = self.run_transaction(self.candidate())
        self.assertTrue(event["accepted"])
        self.assertEqual(event["selected_alpha"], 1)
        self.assertEqual(len(event["backtracking_attempts"]), 1)
        self.assertEqual(len(event["prediction_discrepancy"]), 2)

    def test_all_rejections_restore_same_original(self):
        event = self.run_transaction(self.candidate())
        self.assertFalse(event["accepted"])
        self.assertEqual(len(event["backtracking_attempts"]), 5)
        self.assertEqual(
            tensor_identity(self.original),
            tensor_identity(snapshot_factors(self.model.ara_targets)),
        )
        for row in event["backtracking_attempts"]:
            self.assertEqual(row["restored_hash"], event["before_adapter_hash"])

    def test_nonzero_old_update_reinstalled_is_not_new_update(self):
        self.model.model.layers[0].lora_B["default"].weight.data.fill_(0.01)
        current = snapshot_factors(self.model.ara_targets)
        event = self.run_transaction(current)
        self.assertFalse(event["accepted"])
        self.assertIn(
            "no_effective_change", event["backtracking_attempts"][0]["reasons"]
        )

    def test_monitor_exception_restores_all_factors(self):
        def monitor(model):
            if tensor_identity(
                snapshot_factors(model.ara_targets)
            ) != tensor_identity(self.original):
                raise RuntimeError("评分故障")
            return monitor_score()

        self.context.monitor = monitor
        with self.assertRaisesRegex(RuntimeError, "评分故障"):
            self.run_transaction(self.candidate())
        self.assertEqual(
            tensor_identity(self.original),
            tensor_identity(snapshot_factors(self.model.ara_targets)),
        )


if __name__ == "__main__":
    unittest.main()
