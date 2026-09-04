# SPDX-License-Identifier: AGPL-3.0-or-later

import unittest
from unittest.mock import patch

import torch
from torch import nn

from heretic.ara import ARAOptimizationError, ModuleKey, TargetModule
from heretic.ara_trajectory import (
    TokenizedTrajectoryBatch,
    TrajectoryBoundaries,
    TrajectoryCaptureConfig,
    TrajectoryModuleParameters,
    TrajectoryObservation,
    TrajectoryNonFiniteError,
    TrajectoryOptimizerConfig,
    _canonical_factors,
    build_trajectory_bank,
    causal_capture_positions,
    capture_trajectory_io,
    optimize_trajectory_module,
    prompt_step_weights,
    trajectory_loss,
    valid_continuation_tokens,
)


def observation(offset: float = 0.0) -> TrajectoryObservation:
    prompt_indices = torch.tensor([0, 0, 1, 1], dtype=torch.int64)
    return TrajectoryObservation(
        inputs=torch.tensor([[1.0, 0.0], [0.5, 0.5], [0.0, 1.0], [0.25, 0.75]]),
        outputs=torch.tensor(
            [
                [1.0 + offset, 0.0],
                [0.5 + offset, 0.5],
                [0.0, 1.0 + offset],
                [0.25, 0.75 + offset],
            ]
        ),
        prompt_indices=prompt_indices,
        step_indices=torch.tensor([0, 1, 0, 1], dtype=torch.int16),
        weights=torch.full((4,), 0.5),
    )


class TrajectoryBoundaryTests(unittest.TestCase):
    def test_step_weights_are_prompt_normalized(self) -> None:
        prompts, steps, weights = prompt_step_weights([1, 3], 0.85)
        self.assertEqual(prompts.tolist(), [0, 1, 1, 1])
        self.assertEqual(steps.tolist(), [0, 0, 1, 2])
        for prompt in prompts.unique():
            self.assertAlmostEqual(float(weights[prompts == prompt].sum()), 1.0)

    def test_left_padding_positions_follow_each_row_boundary(self) -> None:
        mask = torch.tensor([[0, 0, 1, 1, 1], [1, 1, 1, 1, 1]])
        positions = causal_capture_positions(mask, [2, 3], [2, 3])
        self.assertEqual(
            positions.tolist(),
            [[0, 3], [0, 4], [1, 2], [1, 3], [1, 4]],
        )

    def test_first_eos_is_included_and_trailing_padding_is_removed(self) -> None:
        self.assertEqual(valid_continuation_tokens([8, 2, 0, 0], 2, 4), (8, 2))
        self.assertEqual(valid_continuation_tokens([8, 9], 2, 2), (8, 9))

    def test_capture_preallocates_step_major_storage_used_by_bank(self) -> None:
        module = nn.Linear(2, 3, bias=False)
        target = TargetModule(
            ModuleKey(0, "attn.o_proj", 0), "layer.proj", module, 2, 3
        )
        inputs = (
            torch.tensor([[[1.0, 0.0], [2.0, 0.0], [3.0, 0.0]]]),
            torch.tensor([[[0.0, 1.0], [0.0, 2.0], [0.0, 3.0]]]),
        )
        batches = [
            TokenizedTrajectoryBatch(lambda value=value: module(value))
            for value in inputs
        ]
        boundaries = [
            TrajectoryBoundaries(
                positions=torch.tensor([[0, 1], [0, 2]]),
                prompt_indices=torch.tensor([index, index], dtype=torch.int64),
                step_indices=torch.tensor([0, 1], dtype=torch.int16),
                weights=torch.tensor([0.6, 0.4], dtype=torch.float32),
            )
            for index in range(2)
        ]
        captured = capture_trajectory_io(
            [target], batches, boundaries, TrajectoryCaptureConfig(2)
        )
        observation = captured[target.key]
        self.assertEqual(observation.step_indices.tolist(), [0, 0, 1, 1])
        bank = build_trajectory_bank(captured, captured)[target.key]
        self.assertEqual(
            bank.steps[0].good_inputs.untyped_storage().data_ptr(),
            observation.inputs.untyped_storage().data_ptr(),
        )

    def test_capture_removes_hooks_when_registration_fails(self) -> None:
        first = nn.Linear(2, 2, bias=False)
        second = nn.Linear(2, 2, bias=False)
        targets = [
            TargetModule(ModuleKey(0, "attn.o_proj", index), name, module, 2, 2)
            for index, (name, module) in enumerate(
                (("layer.first", first), ("layer.second", second))
            )
        ]
        boundary = TrajectoryBoundaries(
            positions=torch.tensor([[0, 0]]),
            prompt_indices=torch.tensor([0], dtype=torch.int64),
            step_indices=torch.tensor([0], dtype=torch.int16),
            weights=torch.tensor([1.0], dtype=torch.float32),
        )
        with (
            patch.object(
                second, "register_forward_hook", side_effect=RuntimeError("boom")
            ),
            self.assertRaisesRegex(RuntimeError, "boom"),
        ):
            capture_trajectory_io(
                targets,
                [TokenizedTrajectoryBatch(lambda: None)],
                [boundary],
                TrajectoryCaptureConfig(1),
            )
        self.assertFalse(first._forward_hooks)


class TrajectoryLossTests(unittest.TestCase):
    def _calibration(self):
        key = ModuleKey(0, "attn.o_proj", 0)
        return build_trajectory_bank(
            {key: observation()},
            {key: observation(0.5)},
        )[key]

    def test_bank_uses_only_same_step_references(self) -> None:
        key = ModuleKey(0, "attn.o_proj", 0)
        bank = build_trajectory_bank(
            {key: observation()},
            {key: observation(0.5)},
        )
        self.assertEqual(set(bank[key].steps), {0, 1})
        self.assertEqual(bank[key].steps[0].good_outputs.shape[0], 2)

    def test_loss_is_finite_and_factor_gradients_exist(self) -> None:
        key = ModuleKey(0, "attn.o_proj", 0)
        calibration = build_trajectory_bank(
            {key: observation()},
            {key: observation(0.5)},
        )[key]
        lora_a = torch.eye(2, requires_grad=True)
        lora_b = torch.zeros((2, 2), requires_grad=True)
        terms = trajectory_loss(
            calibration,
            lora_a,
            lora_b,
            TrajectoryModuleParameters(1.0, 1.0, 2.0, 1.0),
            0.1,
        )
        terms.total.backward()
        self.assertTrue(torch.isfinite(terms.total))
        self.assertIsNotNone(lora_b.grad)

    def test_non_finite_observation_fails_closed(self) -> None:
        invalid = observation()
        invalid.inputs[0, 0] = float("nan")
        with self.assertRaises(ARAOptimizationError):
            invalid.validate()

    def test_negative_trajectory_index_fails_closed(self) -> None:
        invalid = observation()
        invalid.step_indices[0] = -1
        with self.assertRaises(ARAOptimizationError):
            invalid.validate()

    def test_canonicalization_preserves_update_and_gain_is_explicit(self) -> None:
        lora_a = torch.tensor([[1.0, 0.5], [0.25, 1.25]])
        lora_b = torch.tensor([[0.8, 0.1], [0.2, 0.9]])
        canonical_a, canonical_b, _ = _canonical_factors(lora_a, lora_b)
        effective = lora_b @ lora_a
        self.assertTrue(torch.allclose(canonical_b @ canonical_a, effective))
        self.assertTrue(
            torch.allclose((canonical_b * 1.75) @ canonical_a, effective * 1.75)
        )

    def test_deployment_failure_rolls_back_both_factors(self) -> None:
        calibration = self._calibration()
        lora_a = torch.eye(2) * 0.1
        lora_b = torch.eye(2) * 0.1
        original_a, original_b = lora_a.clone(), lora_b.clone()

        def mutate(*args):
            with torch.no_grad():
                args[1].fill_(1.0)
                args[2].fill_(1.0)
            return torch.tensor(1.0), torch.tensor(0.5)

        config = TrajectoryOptimizerConfig(max_good_delta_rms=0.0)
        with patch("heretic.ara_trajectory._run_lbfgs", side_effect=mutate):
            with self.assertRaises(ARAOptimizationError):
                optimize_trajectory_module(
                    calibration,
                    lora_a,
                    lora_b,
                    TrajectoryModuleParameters(1.0, 1.0, 2.0, 1.0),
                    config,
                )
        self.assertTrue(torch.equal(lora_a, original_a))
        self.assertTrue(torch.equal(lora_b, original_b))
        self.assertFalse(lora_a.requires_grad or lora_b.requires_grad)

    def test_non_finite_factor_is_classified_and_rolled_back(self) -> None:
        calibration = self._calibration()
        lora_a = torch.eye(2)
        lora_b = torch.eye(2)
        original_a, original_b = lora_a.clone(), lora_b.clone()

        def inject_nan(*args):
            with torch.no_grad():
                args[1][0, 0] = float("nan")
            return torch.tensor(1.0), torch.tensor(0.5)

        with patch("heretic.ara_trajectory._run_lbfgs", side_effect=inject_nan):
            with self.assertRaises(TrajectoryNonFiniteError):
                optimize_trajectory_module(
                    calibration,
                    lora_a,
                    lora_b,
                    TrajectoryModuleParameters(1.0, 1.0, 2.0, 1.0),
                    TrajectoryOptimizerConfig(),
                )
        self.assertTrue(torch.equal(lora_a, original_a))
        self.assertTrue(torch.equal(lora_b, original_b))


if __name__ == "__main__":
    unittest.main()
