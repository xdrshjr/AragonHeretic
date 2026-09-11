# SPDX-License-Identifier: AGPL-3.0-or-later
"""基座锚定数值目标、monitor 门槛与整组回滚。"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import torch

from heretic.ara_refinement import (
    BlockProposal,
    RefinementNumericalError,
    SolverOptions,
    _block_transaction,
    _canonical_factors,
    _check_effective_update,
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
    def test_high_rank_canonicalization_preserves_effective_update(self):
        devices = ["cpu"] + (["cuda"] if torch.cuda.is_available() else [])
        for device in devices:
            with self.subTest(device=device):
                generator = torch.Generator(device=device).manual_seed(42)
                a = torch.randn(128, 512, generator=generator, device=device)
                b = torch.randn(512, 128, generator=generator, device=device)
                a, b = a / 512**0.5, b * 0.1
                ca, cb, singular = _canonical_factors(a, b)
                _check_effective_update((a, b), (ca, cb))
                self.assertEqual(ca.dtype, a.dtype)
                self.assertEqual(ca.device, a.device)
                reference = b.double() @ a.double()
                actual = cb.double() @ ca.double()
                relative = torch.linalg.vector_norm(actual - reference)
                relative /= torch.linalg.vector_norm(reference)
                self.assertLess(float(relative), 1e-7)
                self.assertTrue(torch.isfinite(singular).all())

    def test_effective_update_check_preserves_cancelling_factor_permutation(self):
        devices = ["cpu"] + (["cuda"] if torch.cuda.is_available() else [])
        for device in devices:
            with self.subTest(device=device):
                # 重排秩维度不改变 BA；FP32 累加却可能丢失中间的 1。
                a = torch.tensor([[1e8], [1.0], [-1e8]], device=device)
                b = torch.ones(1, 3, device=device)
                order = [0, 2, 1]
                _check_effective_update((a, b), (a[order], b[:, order]))

    def test_effective_update_check_rejects_changed_or_nonfinite_factors(self):
        a, b = torch.eye(4), torch.eye(4)
        for value in (1.00001, 1.01, float("nan"), float("inf")):
            with self.subTest(value=value):
                changed = b.clone()
                changed[0, 0] = value
                with self.assertRaises(RefinementNumericalError):
                    _check_effective_update((a, b), (a, changed))

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
