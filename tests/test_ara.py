# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026  Philipp Emanuel Weidmann <pew@worldwidemann.com> + contributors

import unittest
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import torch
from torch import nn

from heretic.ara import (
    ARAArtifacts,
    ARACalibration,
    ARACaptureConfig,
    ARAComponentParameters,
    ARAOptimizationError,
    ARAOptimizationStats,
    ARAOptimizerConfig,
    ARAParameters,
    CalibrationManifest,
    ModuleKey,
    TargetModule,
    _canonicalize,
    build_calibration_bank,
    calculate_ara_loss,
    capture_module_io,
    get_lora_factors,
    optimize_ara_module,
    restore_adapter_state,
    snapshot_adapter_state,
    soft_neighbor_distance,
)
from heretic.model import Model
from heretic.trial_methods import apply_trial, cleanup_trial
from heretic.utils import Prompt


class FakeLoraLinear(nn.Module):
    def __init__(self, in_features: int = 4, out_features: int = 3, rank: int = 2):
        super().__init__()
        self.base_layer = nn.Linear(in_features, out_features, bias=False)
        self.lora_A = nn.ModuleDict(
            {"default": nn.Linear(in_features, rank, bias=False)}
        )
        self.lora_B = nn.ModuleDict(
            {"default": nn.Linear(rank, out_features, bias=False)}
        )
        self.active_adapters = ["default"]
        self.scaling = {"default": 1.0}
        self.use_dora = {"default": False}
        self.fan_in_fan_out = False
        nn.init.zeros_(cast(nn.Linear, self.lora_B["default"]).weight)

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        base = self.base_layer(inputs)
        return base + self.lora_B["default"](self.lora_A["default"](inputs))


def make_target(module: FakeLoraLinear | None = None) -> TargetModule:
    module = module or FakeLoraLinear()
    return TargetModule(
        ModuleKey(0, "attn.o_proj", 0),
        "layers.0.self_attn.o_proj",
        module,
        4,
        3,
    )


def make_calibration() -> ARACalibration:
    torch.manual_seed(4)
    good_inputs = torch.randn(8, 4)
    bad_inputs = torch.randn(8, 4) + 1.5
    matrix = torch.randn(3, 4)
    good_outputs = good_inputs @ matrix.T
    bad_outputs = bad_inputs @ matrix.T
    scale = (good_outputs - good_outputs.mean(0)).square().mean().clamp_min(1e-6)
    return ARACalibration(
        ModuleKey(0, "attn.o_proj", 0),
        good_inputs,
        good_outputs,
        bad_inputs,
        bad_outputs,
        scale,
    )


class ARAMathTests(unittest.TestCase):
    def test_soft_distance_matches_broadcast_reference(self) -> None:
        queries = torch.tensor([[0.0, 1.0], [2.0, 1.0]])
        references = torch.tensor([[0.0, 0.0], [1.0, 1.0]])
        scale = torch.tensor(2.0)
        actual = soft_neighbor_distance(
            queries, references, scale=scale, temperature=0.2
        )
        distances = (queries[:, None] - references).square().mean(2) / scale
        expected = -0.2 * (
            torch.logsumexp(-distances / 0.2, dim=1) - torch.log(torch.tensor(2.0))
        )
        torch.testing.assert_close(actual, expected)

    def test_loss_is_finite_and_differentiable(self) -> None:
        calibration = make_calibration()
        lora_a = torch.randn(2, 4, requires_grad=True)
        lora_b = torch.zeros(3, 2, requires_grad=True)
        parameters = ARAComponentParameters(0.2, 0.5, 1.0)
        loss = calculate_ara_loss(
            calibration, lora_a, lora_b, parameters, temperature=0.1
        )
        loss.total.backward()
        self.assertTrue(torch.isfinite(loss.total))
        self.assertIsNotNone(lora_a.grad)
        self.assertIsNotNone(lora_b.grad)

    def test_loss_terms_match_direct_reference_formula(self) -> None:
        calibration = replace(make_calibration(), balance_reference=torch.tensor(2.0))
        lora_a, lora_b = torch.randn(2, 4), torch.randn(3, 2)
        parameters = ARAComponentParameters(0.2, 0.5, 1.0)
        temperature = 0.1
        actual = calculate_ara_loss(
            calibration, lora_a, lora_b, parameters, temperature=temperature
        )

        def neighbor(query, reference):
            distances = (query[:, None] - reference).square().mean(
                2
            ) / calibration.scale
            return -temperature * (
                torch.logsumexp(-distances / temperature, dim=1)
                - torch.log(torch.tensor(float(len(reference))))
            )

        delta_good = (calibration.good_inputs @ lora_a.T) @ lora_b.T
        new_bad = calibration.bad_outputs + (
            (calibration.bad_inputs @ lora_a.T) @ lora_b.T
        )
        keep = delta_good.square().mean() / calibration.scale
        pull = neighbor(new_bad, calibration.good_outputs).mean()
        bad_distance = neighbor(new_bad, calibration.bad_outputs)
        push = (
            temperature
            * torch.nn.functional.softplus(
                (parameters.margin - bad_distance) / temperature
            )
        ).mean()
        gram = (lora_a @ lora_a.T - lora_b.T @ lora_b).square().mean()
        balance = 1e-4 * gram / cast(torch.Tensor, calibration.balance_reference)
        total = (
            keep
            + parameters.strength * (pull + parameters.push_weight * push)
            + balance
        )
        pairs = (
            (actual.total, total),
            (actual.keep, keep),
            (actual.pull, pull),
            (actual.push, push),
            (actual.balance, balance),
        )
        for observed, expected in pairs:
            torch.testing.assert_close(observed, expected)

    def test_lora_factor_formula_matches_deployed_module(self) -> None:
        module = FakeLoraLinear()
        lora_a, lora_b = get_lora_factors(make_target(module))
        lora_a.data.copy_(torch.randn_like(lora_a))
        lora_b.data.copy_(torch.randn_like(lora_b))
        inputs = torch.randn(5, 4)
        deployed_delta = module(inputs) - module.base_layer(inputs)
        reference_delta = (inputs @ lora_a.T) @ lora_b.T
        torch.testing.assert_close(deployed_delta, reference_delta)

    def test_optimizer_decreases_loss_and_clears_gradients(self) -> None:
        calibration = make_calibration()
        lora_a = torch.randn(2, 4)
        lora_b = torch.zeros(3, 2)
        stats = optimize_ara_module(
            calibration,
            lora_a,
            lora_b,
            ARAComponentParameters(0.2, 0.5, 1.0),
            ARAOptimizerConfig(max_iter=4, history_size=3),
        )
        self.assertLessEqual(stats.final_loss, stats.initial_loss + 1e-6)
        self.assertTrue(torch.isfinite(lora_a).all())
        self.assertTrue(torch.isfinite(lora_b).all())
        self.assertIsNone(lora_a.grad)
        self.assertIsNone(lora_b.grad)

    def test_canonicalization_accepts_high_rank_roundoff(self) -> None:
        torch.manual_seed(0)
        inputs = torch.randn(32, 64)
        lora_a = torch.empty(16, 64)
        torch.nn.init.kaiming_uniform_(lora_a, a=5**0.5)
        lora_b = torch.randn(64, 16)
        calibration = ARACalibration(
            ModuleKey(0, "attn.o_proj", 0),
            inputs[:16],
            torch.zeros(16, 64),
            inputs[16:],
            torch.zeros(16, 64),
            torch.tensor(1.0),
        )
        before = (inputs @ lora_a.T) @ lora_b.T

        _canonicalize(calibration, lora_a, lora_b)

        after = (inputs @ lora_a.T) @ lora_b.T
        relative_error = torch.linalg.vector_norm(after - before) / (
            torch.linalg.vector_norm(before)
        )
        self.assertLess(float(relative_error), 1e-5)

    def test_optimizer_rolls_back_on_failure(self) -> None:
        calibration = make_calibration()
        lora_a = torch.randn(2, 4)
        lora_b = torch.zeros(3, 2)
        before_a, before_b = lora_a.clone(), lora_b.clone()
        with patch.object(torch.optim.LBFGS, "step", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                optimize_ara_module(
                    calibration,
                    lora_a,
                    lora_b,
                    ARAComponentParameters(0.2, 0.5, 1.0),
                    ARAOptimizerConfig(max_iter=2, history_size=2),
                )
        torch.testing.assert_close(lora_a, before_a, rtol=0, atol=0)
        torch.testing.assert_close(lora_b, before_b, rtol=0, atol=0)

    def test_deployed_loss_regression_rolls_back(self) -> None:
        calibration = make_calibration()
        lora_a, lora_b = torch.randn(2, 4), torch.zeros(3, 2)
        before = lora_a.clone(), lora_b.clone()

        def regress(_calibration, _a, candidate_b):
            candidate_b.data.fill_(10)
            return 0.0, 1.0

        with (
            patch("heretic.ara._run_optimizer", return_value=(0.0, 0.0, 1)),
            patch("heretic.ara._canonicalize", side_effect=regress),
            self.assertRaisesRegex(ARAOptimizationError, "did not decrease"),
        ):
            optimize_ara_module(
                calibration,
                lora_a,
                lora_b,
                ARAComponentParameters(0.2, 0.5, 1.0),
                ARAOptimizerConfig(max_iter=2, history_size=2),
            )
        torch.testing.assert_close(lora_a, before[0], rtol=0, atol=0)
        torch.testing.assert_close(lora_b, before[1], rtol=0, atol=0)

    def test_zero_strength_is_an_exact_noop(self) -> None:
        calibration = make_calibration()
        lora_a, lora_b = torch.randn(2, 4), torch.zeros(3, 2)
        before = lora_a.clone(), lora_b.clone()
        with patch("heretic.ara._make_optimizer") as make_optimizer:
            stats = optimize_ara_module(
                calibration,
                lora_a,
                lora_b,
                ARAComponentParameters(0.0, 2.0, 4.0),
                ARAOptimizerConfig(max_iter=2, history_size=2),
            )
        make_optimizer.assert_not_called()
        self.assertEqual(stats.closure_calls, 0)
        torch.testing.assert_close(lora_a, before[0], rtol=0, atol=0)
        torch.testing.assert_close(lora_b, before[1], rtol=0, atol=0)

    def test_nonfinite_loss_rolls_back(self) -> None:
        calibration = make_calibration()
        calibration = replace(
            calibration,
            bad_outputs=calibration.bad_outputs.fill_(float("nan")),
        )
        lora_a, lora_b = torch.randn(2, 4), torch.zeros(3, 2)
        before = lora_a.clone(), lora_b.clone()
        with self.assertRaises(ARAOptimizationError):
            optimize_ara_module(
                calibration,
                lora_a,
                lora_b,
                ARAComponentParameters(0.2, 0.5, 1.0),
                ARAOptimizerConfig(max_iter=2, history_size=2),
            )
        torch.testing.assert_close(lora_a, before[0], rtol=0, atol=0)
        torch.testing.assert_close(lora_b, before[1], rtol=0, atol=0)


class ARAStateAndCaptureTests(unittest.TestCase):
    def test_rejects_non_unit_adapter_semantics(self) -> None:
        target = make_target()
        target.module.scaling["default"] = 0.5  # type: ignore[attr-defined]
        with self.assertRaisesRegex(ARAOptimizationError, "unit scaling"):
            get_lora_factors(target)

    def test_snapshot_restores_both_factors_deterministically(self) -> None:
        target = make_target()
        state = snapshot_adapter_state((target,), 42)
        lora_a, lora_b = get_lora_factors(target)
        lora_a.data.add_(1)
        lora_b.data.add_(1)
        restore_adapter_state((target,), state)
        for factor, name in zip((lora_a, lora_b), state.ordered_parameter_names):
            torch.testing.assert_close(factor, state.tensors[name], rtol=0, atol=0)
        repeated = snapshot_adapter_state((target,), 42)
        for name in state.ordered_parameter_names:
            torch.testing.assert_close(state.tensors[name], repeated.tensors[name])

    def test_intervening_trial_cannot_change_parameter_replay(self) -> None:
        target = make_target()
        state = snapshot_adapter_state((target,), 42)
        calibration = make_calibration()
        artifacts = ARAArtifacts(
            {target.key: calibration},
            state,
            CalibrationManifest((), (), (), (), 42, 1),
            "model",
            "study",
            (target,),
            ARAOptimizerConfig(max_iter=2, history_size=2),
        )
        model = cast(
            Model,
            SimpleNamespace(
                settings=SimpleNamespace(print_debug_information=False),
                reset_model=lambda: None,
            ),
        )

        def mutate(_calibration, lora_a, lora_b, parameters, _config):
            lora_a.data.add_(parameters.strength)
            lora_b.data.add_(parameters.strength)
            return ARAOptimizationStats(1, 0, 1, 8, 8, 1, 1, 1, 0, 0)

        def replay(strength):
            parameters = ARAParameters(
                0, 1, {"attn.o_proj": ARAComponentParameters(strength, 0.5, 1.0)}
            )
            apply_trial(model, parameters, artifacts)
            factors = tuple(
                value.detach().clone() for value in get_lora_factors(target)
            )
            cleanup_trial(model, artifacts)
            return factors

        with patch("heretic.trial_methods.optimize_ara_module", side_effect=mutate):
            first = replay(0.2)
            replay(0.8)
            repeated = replay(0.2)
        for before, after in zip(first, repeated):
            torch.testing.assert_close(before, after, rtol=0, atol=0)

    def test_capture_uses_cpu_float32_and_removes_hooks(self) -> None:
        module = FakeLoraLinear()
        target = make_target(module)
        prompts = [Prompt("system", "one"), Prompt("system", "two")]

        def generate(batch: list[Prompt]) -> None:
            module(torch.ones(len(batch), 2, 4, dtype=torch.float16).float())

        captured = capture_module_io(
            (target,), prompts, generate, ARACaptureConfig(batch_size=1)
        )
        observation = captured[target.key]
        self.assertEqual(observation.inputs.device.type, "cpu")
        self.assertEqual(observation.inputs.dtype, torch.float32)
        self.assertEqual(len(observation.inputs), 2)
        self.assertFalse(module._forward_hooks)

    def test_capture_removes_hooks_when_generation_fails(self) -> None:
        module = FakeLoraLinear()
        target = make_target(module)

        def fail(batch: list[Prompt]) -> None:
            del batch
            raise RuntimeError("generation failed")

        with self.assertRaisesRegex(RuntimeError, "generation failed"):
            capture_module_io(
                (target,),
                [Prompt("system", "one")],
                fail,
                ARACaptureConfig(),
            )
        self.assertFalse(module._forward_hooks)

    def test_capture_removes_hooks_when_registration_fails(self) -> None:
        first, second = FakeLoraLinear(), FakeLoraLinear()
        targets = (
            make_target(first),
            replace(
                make_target(second),
                key=ModuleKey(0, "mlp.down_proj", 0),
                full_name="layers.0.mlp.down_proj",
            ),
        )
        with (
            patch.object(
                second, "register_forward_hook", side_effect=RuntimeError("register")
            ),
            self.assertRaisesRegex(RuntimeError, "register"),
        ):
            capture_module_io(
                targets, [Prompt("system", "one")], lambda _: None, ARACaptureConfig()
            )
        self.assertFalse(first._forward_hooks)

    def test_calibration_rejects_insufficient_samples(self) -> None:
        module = FakeLoraLinear()
        target = make_target(module)

        def generate(batch: list[Prompt]) -> None:
            module(torch.ones(len(batch), 2, 4))

        captured = capture_module_io(
            (target,), [Prompt("system", "one")], generate, ARACaptureConfig()
        )
        with self.assertRaisesRegex(RuntimeError, "at least two"):
            build_calibration_bank(captured, captured)

    def test_optimizer_rejects_malformed_calibration(self) -> None:
        calibration = make_calibration()
        malformed = replace(calibration, good_outputs=calibration.good_outputs[:-1])
        with self.assertRaisesRegex(ARAOptimizationError, "shapes do not match"):
            optimize_ara_module(
                malformed,
                torch.randn(2, 4),
                torch.zeros(3, 2),
                ARAComponentParameters(0.2, 0.5, 1.0),
                ARAOptimizerConfig(max_iter=2, history_size=2),
            )


if __name__ == "__main__":
    unittest.main()
