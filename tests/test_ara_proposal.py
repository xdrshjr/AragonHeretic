"""有效权重投影与回溯的独立稠密数值参考。"""

import unittest

import torch

from heretic.ara_proposal import (
    ProposalPolicy,
    compare_effective_factors,
    effective_distance,
    interpolate_update,
    project_proposal,
)


class ProposalTests(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(1)
        self.a = torch.eye(3)
        self.b = torch.diag(torch.tensor([27.630745, 13.190416, 2.0]))
        self.policy = ProposalPolicy(zero_initialization=(self.a, self.b * 0))

    def test_pilot_spectrum_is_clipped_without_raising_guard(self):
        result = project_proposal((self.a, self.b), self.policy)
        a, b = result.factors
        expected = torch.diag(torch.tensor([7.999992, 7.999992, 2.0]))
        torch.testing.assert_close(b @ a, expected)
        self.assertLessEqual(float(torch.linalg.svdvals(b @ a).max()), 8)
        self.assertEqual(result.statistics["clipped_singular_values"], 2)

    def test_scale_preserves_relative_spectrum(self):
        policy = ProposalPolicy("scale-v1")
        a, b = project_proposal((self.a, self.b), policy).factors
        torch.testing.assert_close(b @ a, self.b * (7.999992 / 27.630745))

    def test_interpolation_matches_weight_product_not_factors(self):
        old = (self.a, torch.eye(3))
        new = (-self.a, -torch.eye(3) * 3)
        a, b = interpolate_update(old, new, 0.5, self.policy).factors
        torch.testing.assert_close(b @ a, torch.eye(3) * 2)

    def test_rank_truncation_matches_dense_reference(self):
        old = (torch.tensor([[1.0, 0.0]]), torch.tensor([[2.0], [0.0]]))
        new = (torch.tensor([[0.0, 1.0]]), torch.tensor([[0.0], [4.0]]))
        result = interpolate_update(old, new, 0.5, ProposalPolicy())
        a, b = result.factors
        torch.testing.assert_close(b @ a, torch.diag(torch.tensor([0.0, 2.0])))
        self.assertAlmostEqual(
            result.statistics["truncation_frobenius_error"], 1
        )

    def test_zero_keeps_nonzero_initial_a(self):
        a, b = project_proposal((self.a, self.b * 0), self.policy).factors
        self.assertTrue(torch.equal(a, self.a))
        self.assertEqual(int(b.count_nonzero()), 0)

    def test_nonfinite_is_not_clipped_into_valid_candidate(self):
        self.b[0, 0] = float("nan")
        with self.assertRaisesRegex(RuntimeError, "raw_nonfinite"):
            project_proposal((self.a, self.b), self.policy)

    def test_factor_basis_change_is_not_effective_change(self):
        old = (self.a, self.b * 0.1)
        new = (-self.a, -self.b * 0.1)
        self.assertLess(effective_distance(old, new), 1e-12)
        compare_effective_factors(
            {"x.A": old[0], "x.B": old[1]}, {"x.A": new[0], "x.B": new[1]}
        )

    def test_unregistered_alpha_and_invalid_old_state_fail(self):
        with self.assertRaises(ValueError):
            interpolate_update(
                (self.a, self.b), (self.a, self.b), 0, self.policy
            )
        with self.assertRaisesRegex(ValueError, "起点"):
            interpolate_update(
                (self.a, self.b), (self.a, self.b), 1, self.policy
            )


if __name__ == "__main__":
    unittest.main()
