# SPDX-License-Identifier: AGPL-3.0-or-later
"""在小矩阵上投影和插值有效更新；不构造完整 GPU 权重矩阵。"""

from __future__ import annotations

from dataclasses import dataclass
import math

import torch


@dataclass(frozen=True)
class ProposalPolicy:
    """冻结的部署策略及原始零权重初始化。"""

    name: str = "spectral-backtrack-v1"
    margin: float = 1e-6
    zero_initialization: tuple | None = None

    def __post_init__(self):
        if (
            self.name
            not in {
                "reject-v1",
                "scale-v1",
                "spectral-clip-v1",
                "spectral-backtrack-v1",
            }
            or self.margin != 1e-6
        ):
            raise ValueError("提案策略或谱投影余量未注册")


@dataclass(frozen=True)
class ProjectedProposal:
    """FP32 因子及投影的原始、最终数值证据。"""

    factors: tuple
    statistics: dict


def validate_factors(factors):
    """验证 A/B 形状、精度和有限性；返回部署秩。"""
    a, b = factors
    if a.ndim != 2 or b.ndim != 2 or a.shape[0] != b.shape[1]:
        raise ValueError("有效更新因子形状不匹配")
    if a.device != b.device or a.dtype != b.dtype:
        raise ValueError("有效更新因子设备或精度不匹配")
    if min(*a.shape, *b.shape) <= 0:
        raise ValueError("有效更新因子不能为空")
    if not all(torch.isfinite(value).all() for value in factors):
        raise RuntimeError("raw_nonfinite")
    return a.shape[0]


@torch.no_grad()
def _spectrum(factors):
    validate_factors(factors)
    a, b = (value.detach().double() for value in factors)
    qb, rb = torch.linalg.qr(b, mode="reduced")
    qa, ra = torch.linalg.qr(a.T, mode="reduced")
    u, singular, vh = torch.linalg.svd(rb @ ra.T, full_matrices=False)
    return qb @ u, singular, vh @ qa.T


def effective_norm(factors) -> float:
    """以小矩阵奇异值计算有效更新 Frobenius 范数。"""
    return float(torch.linalg.vector_norm(_spectrum(factors)[1]))


@torch.no_grad()
def effective_distance(previous, current) -> float:
    """拼接差分因子计算 BA 差值，避免因基底变化误报更新。"""
    validate_factors(previous)
    validate_factors(current)
    if previous[0].shape[1] != current[0].shape[1] or (
        previous[1].shape[0] != current[1].shape[0]
    ):
        raise ValueError("比较因子部署形状不一致")
    a = torch.cat((previous[0].double(), current[0].double()), dim=0)
    b = torch.cat((-previous[1].double(), current[1].double()), dim=1)
    return effective_norm((a, b))


def relative_change(previous, current) -> float:
    """使用冻结的 max(1, ||D_old||F) 分母。"""
    return effective_distance(previous, current) / max(
        1.0, effective_norm(previous)
    )


def _balanced(spectrum, rank, policy):
    u, singular, vh = spectrum
    active = min(rank, singular.numel())
    roots = singular[:active].sqrt()
    left, right = u[:, :active], vh[:active]
    # 固定符号，重复奇异值的子空间旋转仍按有效权重比较。
    pivots = left.abs().argmax(dim=0)
    signs = left[pivots, torch.arange(active, device=left.device)].sign()
    signs = torch.where(signs == 0, torch.ones_like(signs), signs)
    a = torch.zeros(rank, vh.shape[1], device=vh.device, dtype=torch.float32)
    b = torch.zeros(u.shape[0], rank, device=u.device, dtype=torch.float32)
    a[:active] = (roots[:, None] * right * signs[:, None]).float()
    b[:, :active] = (left * roots * signs).float()
    if not torch.count_nonzero(singular):
        if policy.zero_initialization is None:
            raise ValueError("零模块需要冻结的非零 A 初始化")
        initial_a, initial_b = policy.zero_initialization
        if initial_a.shape != a.shape or initial_b.shape != b.shape:
            raise ValueError("零模块初始化形状不匹配")
        if not torch.isfinite(initial_a).all() or not initial_a.count_nonzero():
            raise ValueError("零模块初始化 A 必须有限且非零")
        a = initial_a.detach().to(a).clone()
        b.zero_()
    return a, b


def _statistics(raw, final, singular, clipped):
    final_singular = _spectrum(final)[1]
    a, b = final
    paired_zero = (a == 0).all(dim=1) & (b == 0).all(dim=0)
    raw_norm = float(torch.linalg.vector_norm(singular))
    return {
        "raw_spectral_norm": float(singular.max()),
        "projected_spectral_norm": float(final_singular.max()),
        "raw_frobenius_norm": raw_norm,
        "projected_frobenius_norm": effective_norm(final),
        "relative_projection_change": effective_distance(raw, final)
        / max(raw_norm, 1e-30),
        "clipped_singular_values": clipped,
        "actual_rank": int(torch.count_nonzero(final_singular > 1e-12)),
        "paired_zero_directions": int(paired_zero.sum()),
        "svd_basis_unique": False,
    }


@torch.no_grad()
def project_proposal(factors, policy) -> ProjectedProposal:
    """谱裁剪或统一缩放；无变换 reject 路径保持原因子。"""
    rank = validate_factors(factors)
    u, singular, vh = _spectrum(factors)
    ceiling = 8.0 * (1 - policy.margin)
    clipped = int((singular > ceiling).sum())
    target = singular.clone()
    if policy.name == "scale-v1":
        scale = (
            min(1.0, ceiling / float(singular.max())) if singular.max() else 1
        )
        target *= scale
    elif policy.name != "reject-v1":
        target.clamp_(max=ceiling)
    final = (
        tuple(value.detach().float().clone() for value in factors)
        if policy.name == "reject-v1"
        else _balanced((u, target, vh), rank, policy)
    )
    _verify_reconstruction(final, (u, target, vh))
    return ProjectedProposal(
        final, _statistics(factors, final, singular, clipped)
    )


@torch.no_grad()
def interpolate_update(previous, proposal, alpha, policy):
    """插值权重乘积，截断秩并裁剪；禁止 A/B 分别线性插值。"""
    rank = validate_factors(previous)
    validate_factors(proposal)
    if not math.isfinite(alpha) or alpha not in (1, 0.5, 0.25, 0.125, 0.0625):
        raise ValueError("回溯 alpha 未注册")
    if any(x.shape != y.shape for x, y in zip(previous, proposal)):
        raise ValueError("回溯起点和提案形状不同")
    if float(_spectrum(previous)[1].max()) > 8.0:
        raise ValueError("回溯起点违反谱约束")
    a = torch.cat(
        (
            previous[0].double() * math.sqrt(1 - alpha),
            proposal[0].double() * math.sqrt(alpha),
        ),
        dim=0,
    )
    b = torch.cat(
        (
            previous[1].double() * math.sqrt(1 - alpha),
            proposal[1].double() * math.sqrt(alpha),
        ),
        dim=1,
    )
    u, singular, vh = _spectrum((a, b))
    target = singular.clone()
    target[rank:] = 0
    target.clamp_(max=8.0 * (1 - policy.margin))
    final = _balanced((u, target, vh), rank, policy)
    _verify_reconstruction(final, (u, target, vh))
    stats = _statistics(
        (a, b), final, singular, int((target != singular).sum())
    )
    stats.update(
        alpha=alpha,
        pre_truncation_rank=int((singular > 1e-12).sum()),
        truncation_frobenius_error=float(
            torch.linalg.vector_norm(singular - target)
        ),
        relative_effective_change=relative_change(previous, final),
    )
    return ProjectedProposal(final, stats)


def _verify_reconstruction(factors, spectrum):
    """FP64 分行核对预定投影目标，FP32 重建超差立即失败。"""
    a, b = (value.detach().cpu().double() for value in factors)
    u, singular, vh = (value.detach().cpu() for value in spectrum)
    for start in range(0, b.shape[0], 128):
        expected = (u[start : start + 128] * singular) @ vh
        actual = b[start : start + 128] @ a
        if not torch.allclose(actual, expected, rtol=1e-5, atol=1e-6):
            raise RuntimeError("projection_reconstruction_mismatch")


@torch.no_grad()
def compare_effective_factors(left, right) -> None:
    """重放逐模块 FP64 分行比较有效权重，独立验证部署约束。"""
    if left.keys() != right.keys():
        raise ValueError("重放模块清单不同")
    for key in sorted(name for name in left if name.endswith(".A")):
        bkey = key[:-1] + "B"
        pairs = [(snapshot[key], snapshot[bkey]) for snapshot in (left, right)]
        for factors in pairs:
            if validate_factors(factors) > 128:
                raise ValueError("重放部署 rank 超限")
            if float(_spectrum(factors)[1].max()) > 8.0:
                raise ValueError("重放部署谱约束超限")
        if any(x.shape != y.shape for x, y in zip(*pairs)):
            raise ValueError("重放部署形状不同")
        for start in range(0, pairs[0][1].shape[0], 128):
            values = [
                b[start : start + 128].double() @ a.double() for a, b in pairs
            ]
            if not torch.allclose(*values, rtol=1e-5, atol=1e-6):
                raise ValueError("重放有效权重与锁定候选不一致")
