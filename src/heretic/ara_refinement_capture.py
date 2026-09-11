# SPDX-License-Identifier: AGPL-3.0-or-later
"""固定 token 的当前状态/全基座配对捕获及事务上下文。"""

from __future__ import annotations

import hashlib
import math
from contextlib import contextmanager
from dataclasses import dataclass, replace

import torch
from torch import Tensor

from .ara import TargetModule, get_lora_factors
from .ara_research_schema import digest
from .ara_trajectory import (
    TokenizedTrajectoryBatch,
    TrajectoryBoundaries,
    TrajectoryCaptureConfig,
    _step_scale,
    capture_trajectory_io,
)


@dataclass(frozen=True)
class TokenSequence:
    """保留原始 token IDs，禁止 decode 后重新分词。"""

    prompt_id: str
    prompt_tokens: tuple[int, ...]
    continuation: tuple[int, ...]


@dataclass(frozen=True)
class PairedObservation:
    """当前输入、禁用层组输出和全基座参考的对齐行。"""

    inputs: Tensor
    minus: Tensor
    reference: Tensor
    prompt_indices: Tensor
    step_indices: Tensor
    weights: Tensor


@dataclass(frozen=True)
class ReferencePair:
    """一个模块的双侧基座锚定 bank。"""

    good: PairedObservation
    bad: PairedObservation
    scales: dict[int, float]


@dataclass(frozen=True)
class ReferenceBank:
    """结构来源和实际张量字节分别标识。"""

    pairs: dict
    manifest: dict
    capture_manifest_hash: str
    tensor_content_hash: str


@dataclass(frozen=True)
class CaptureRequest:
    """一组固定轨迹及当前/基座/协议身份。"""

    targets: tuple[TargetModule, ...]
    sequences: dict[str, tuple[TokenSequence, ...]]
    protocol_hash: str
    base_identity: str
    capture_mode: str = "sequential"


def snapshot_factors(targets) -> dict[str, Tensor]:
    """复制独立 CPU FP32 因子，不保留对模型权重的引用。"""
    result = {}
    for target in targets:
        for suffix, factor in zip(("A", "B"), get_lora_factors(target)):
            result[f"{target.full_name}.{suffix}"] = (
                factor.detach().cpu().clone()
            )
    return result


def tensor_identity(tensors: dict[str, Tensor]) -> str:
    """同时绑定名称、shape、dtype 和字节。"""
    result = hashlib.sha256()
    for name in sorted(tensors):
        value = tensors[name].detach().cpu().contiguous()
        result.update(
            digest([name, list(value.shape), str(value.dtype)]).encode()
        )
        result.update(value.view(torch.uint8).numpy().tobytes())
    return result.hexdigest()


class SnapshotRestoreError(RuntimeError):
    """因子复制失败后模型状态不可信，必须终止本次进程。"""


def apply_snapshot(targets, snapshot: dict[str, Tensor], expected_hash=None):
    """先验证整份快照再绝对替换，禁止部分应用与 BA 累加。"""
    if expected_hash is not None and tensor_identity(snapshot) != expected_hash:
        raise ValueError("adapter 快照内容 hash 不匹配")
    assignments = []
    for target in targets:
        for suffix, factor in zip(("A", "B"), get_lora_factors(target)):
            name = f"{target.full_name}.{suffix}"
            value = snapshot[name]
            if value.shape != factor.shape or value.dtype != torch.float32:
                raise ValueError(f"adapter 快照 shape/dtype 不匹配：{name}")
            if not torch.isfinite(value).all():
                raise ValueError(f"adapter 快照包含非有限值：{name}")
            assignments.append((factor, value))
    if len(assignments) != len(snapshot):
        raise ValueError("adapter 快照模块身份不匹配")
    try:
        with torch.no_grad():
            for factor, value in assignments:
                factor.copy_(value)
                factor.grad = None
                factor.requires_grad_(False)
    except RuntimeError as error:
        raise SnapshotRestoreError("因子复制失败，禁止继续使用模型") from error


@contextmanager
def disabled_targets(targets):
    """只临时清零 B；异常退出也恢复完整 A/B 和原激活开关。"""
    state = snapshot_factors(targets)
    flags = [
        (target.module, getattr(target.module, "_disable_adapters", None))
        for target in targets
    ]
    try:
        with torch.no_grad():
            for target in targets:
                get_lora_factors(target)[1].zero_()
        yield
    finally:
        try:
            apply_snapshot(targets, state)
        finally:
            for module, flag in flags:
                if flag is not None:
                    module._disable_adapters = flag


@contextmanager
def evaluation_mode(model):
    """暂时关闭 dropout，并逐模块恢复原有 training 标志。"""
    network = model.model
    states = [(module, module.training) for module in network.modules()]
    network.eval()
    try:
        yield
    finally:
        for module, training in states:
            module.training = training


def causal_positions(mask: Tensor, prompt_lengths, continuation_lengths):
    """支持左右 padding；EOS 预测位置计入，padding 不计入。"""
    if mask.ndim != 2 or len(prompt_lengths) != mask.shape[0]:
        raise ValueError("轨迹 batch 边界数量不匹配")
    if len(continuation_lengths) != mask.shape[0]:
        raise ValueError("轨迹 continuation 数量不匹配")
    positions = []
    for row, (prompt, length) in enumerate(
        zip(prompt_lengths, continuation_lengths, strict=True)
    ):
        indices = mask[row].nonzero().flatten()
        if prompt < 1 or length < 1 or len(indices) != prompt + length - 1:
            raise ValueError("轨迹长度与 attention mask 不匹配")
        if not torch.equal(
            indices,
            torch.arange(indices[0], indices[-1] + 1, device=indices.device),
        ):
            raise ValueError("attention mask 必须连续")
        positions.extend(
            (row, int(indices[prompt - 1 + step])) for step in range(length)
        )
    return torch.tensor(positions, dtype=torch.int64)


def _trim_continuation(tokens, eos, limit):
    eos_ids = set(eos if isinstance(eos, (list, tuple)) else [eos])
    selected = []
    for token in tokens[:limit]:
        selected.append(int(token))
        if token in eos_ids:
            break
    if not selected:
        raise ValueError("轨迹至少需要一个有效预测位置")
    return tuple(selected)


def generate_sequences(model, prompts: list, identities: list[str], limit=8):
    """一次一题生成并保存 token，明确拒绝上下文截断。"""
    if len(prompts) != len(identities) or not prompts:
        raise ValueError("轨迹题目身份数量不匹配")
    result = []
    with evaluation_mode(model):
        for prompt, prompt_id in zip(prompts, identities, strict=True):
            inputs, output = model.generate(
                [prompt],
                max_new_tokens=limit,
                do_sample=False,
                num_beams=1,
            )
            generated = getattr(output, "sequences", output)
            if generated.shape[0] != 1:
                raise ValueError("轨迹生成响应数量不匹配")
            mask = inputs["attention_mask"][0].bool()
            tokens = tuple(inputs["input_ids"][0][mask].cpu().tolist())
            width = inputs["input_ids"].shape[1]
            context_limit = getattr(
                model.model.config, "max_position_embeddings", 0
            )
            if context_limit and len(tokens) + limit > context_limit:
                raise ValueError("输入加生成预算超过上下文上限")
            continuation = _trim_continuation(
                generated[0, width:].cpu().tolist(),
                model.tokenizer.eos_token_id,
                limit,
            )
            result.append(TokenSequence(prompt_id, tokens, continuation))
    return tuple(result)


def sequence_identity(sequences: dict) -> str:
    """捕获结构身份包含每题的完整输入和 continuation。"""
    return digest(
        {side: [vars(row) for row in rows] for side, rows in sequences.items()}
    )


def _build_capture_calls(model, sequences):
    batches, boundaries = [], []
    device = model.model.get_input_embeddings().weight.device
    for index, sequence in enumerate(sequences):
        tokens = sequence.prompt_tokens + sequence.continuation[:-1]
        if not tokens or not sequence.continuation:
            raise ValueError("固定轨迹不能没有 token")
        inputs = torch.tensor([tokens], dtype=torch.long, device=device)
        positions = torch.tensor(
            [
                (0, len(sequence.prompt_tokens) - 1 + step)
                for step in range(len(sequence.continuation))
            ]
        )
        weights = torch.tensor(
            [0.85**step for step in range(len(sequence.continuation))]
        )
        weights /= weights.sum()
        boundaries.append(
            TrajectoryBoundaries(
                positions,
                torch.full((len(weights),), index, dtype=torch.int64),
                torch.arange(len(weights), dtype=torch.int16),
                weights,
            )
        )

        def forward(input_ids=inputs):
            with torch.inference_mode():
                return model.model(
                    input_ids=input_ids,
                    attention_mask=torch.ones_like(input_ids),
                    use_cache=False,
                )

        batches.append(TokenizedTrajectoryBatch(forward))
    return batches, boundaries


def capture_fixed_sequences(model, targets, sequences):
    """对固定 token 运行真实前向，按模块捕获 CPU FP32 I/O。"""
    batches, boundaries = _build_capture_calls(model, sequences)
    limit = max(len(row.continuation) for row in sequences)
    with evaluation_mode(model):
        return capture_trajectory_io(
            targets,
            batches,
            boundaries,
            TrajectoryCaptureConfig(limit),
        )


def _align_pair(current, reference):
    for name in ("prompt_indices", "step_indices", "weights"):
        if not torch.equal(getattr(current, name), getattr(reference, name)):
            raise ValueError(f"当前/基座捕获 {name} 不对齐")
    if current.outputs.shape != reference.outputs.shape:
        raise ValueError("当前/基座输出 shape 不一致")
    return PairedObservation(
        current.inputs,
        current.outputs,
        reference.outputs,
        current.prompt_indices,
        current.step_indices,
        current.weights,
    )


def _filter_steps(pair, steps):
    mask = torch.tensor([int(step) in steps for step in pair.step_indices])
    values = {
        name: getattr(pair, name)[mask].clone()
        for name in PairedObservation.__dataclass_fields__
    }
    for prompt in values["prompt_indices"].unique():
        selected = values["prompt_indices"] == prompt
        values["weights"][selected] /= values["weights"][selected].sum()
    return PairedObservation(**values)


def _build_reference_pair(good, bad):
    counts = [
        dict(zip(*torch.unique(row.step_indices, return_counts=True)))
        for row in (good, bad)
    ]
    counts = [{int(k): int(v) for k, v in count.items()} for count in counts]
    steps = {
        step
        for step, count in counts[0].items()
        if count >= 2 and counts[1].get(step, 0) >= 2
    }
    if 0 not in steps:
        raise ValueError("step 0 任一侧不足两个参考")
    good, bad = _filter_steps(good, steps), _filter_steps(bad, steps)
    scales = {
        step: _step_scale(good.reference[good.step_indices == step])
        for step in sorted(steps)
    }
    return ReferencePair(good, bad, scales)


def capture_reference_pair(model, request: CaptureRequest) -> ReferenceBank:
    """组外保持已接受状态；基座参考总是禁用全体 adapter。"""
    before = tensor_identity(snapshot_factors(model.ara_targets))
    paired = {}
    with disabled_targets(request.targets):
        current = {
            side: capture_fixed_sequences(model, request.targets, rows)
            for side, rows in request.sequences.items()
        }
    with disabled_targets(model.ara_targets):
        reference = {
            side: capture_fixed_sequences(model, request.targets, rows)
            for side, rows in request.sequences.items()
        }
    for target in request.targets:
        key = target.key
        good = _align_pair(current["good"][key], reference["good"][key])
        bad = _align_pair(current["bad"][key], reference["bad"][key])
        paired[key] = _build_reference_pair(good, bad)
    if tensor_identity(snapshot_factors(model.ara_targets)) != before:
        raise RuntimeError("配对捕获未恢复 adapter 状态")
    manifest = _capture_manifest(request, before, paired)
    tensors = {}
    for key, pair in paired.items():
        for side in ("good", "bad"):
            for name in PairedObservation.__dataclass_fields__:
                tensors[f"{key}.{side}.{name}"] = getattr(
                    getattr(pair, side), name
                )
    return ReferenceBank(
        paired, manifest, digest(manifest), tensor_identity(tensors)
    )


def _capture_manifest(request, state, pairs):
    return {
        "protocol_hash": request.protocol_hash,
        "base_identity": request.base_identity,
        "capture_mode": request.capture_mode,
        "captured_state_hash": state,
        "evaluation_state_hash": state,
        "disable_policy": "block-B-zero/base-all-B-zero",
        "continuation_hash": sequence_identity(request.sequences),
        "sequences": {
            side: [vars(row) for row in rows]
            for side, rows in request.sequences.items()
        },
        "blocks": [
            {
                "key": vars(target.key),
                "name": target.full_name,
                "shape": [target.out_features, target.in_features],
            }
            for target in request.targets
        ],
        "steps": {str(key): sorted(pair.scales) for key, pair in pairs.items()},
        "dtype": "float32",
    }


def bind_evaluation_state(bank: ReferenceBank, state_hash: str):
    """只有显式冻结诊断允许捕获来源与本次评估状态不同。"""
    captured = bank.manifest["captured_state_hash"]
    if captured != state_hash:
        if bank.manifest["capture_mode"] != "frozen-diagnostic":
            raise ValueError("顺序模式禁止跨 adapter 状态复用 bank")
    manifest = {**bank.manifest, "evaluation_state_hash": state_hash}
    return replace(
        bank, manifest=manifest, capture_manifest_hash=digest(manifest)
    )


def preflight_snapshot_storage(model, root, *, snapshots=32):
    """按实际 shape 预留全部 trial、重放及 staging 快照和 20% 余量。"""
    import shutil

    factor_bytes = sum(
        factor.numel() * 4
        for target in model.ara_targets
        for factor in get_lora_factors(target)
    )
    if type(snapshots) is not int or snapshots < 1:
        raise ValueError("快照预留数量必须为正整数")
    required = math.ceil(factor_bytes * snapshots * 1.2)
    available = shutil.disk_usage(root).free
    if available < required:
        raise RuntimeError(
            f"快照磁盘不足：需要 {required} 字节，可用 {available}"
        )
    return {
        "snapshot_bytes": factor_bytes,
        "required_bytes": required,
        "available_bytes": available,
    }


def research_resource_status(model):
    """每组边界记录 allocated/reserved/实际可用显存和进程 RSS。"""
    import psutil

    rss = psutil.Process().memory_info().rss / 1024**3
    if rss > model.settings.ara_v3.max_cpu_rss_gib:
        raise RuntimeError("研究进程 CPU RSS 超过冻结上限")
    devices = model.settings.ara_runtime_guard.required_target_devices
    gpu = {}
    for device in devices:
        if not device.startswith("cuda:"):
            continue
        index = int(device.split(":")[1])
        allocated = torch.cuda.max_memory_allocated(index) / 1024**3
        reserved = torch.cuda.max_memory_reserved(index) / 1024**3
        free, total = torch.cuda.mem_get_info(index)
        if allocated > model.settings.ara_runtime_guard.max_cuda_allocated_gib:
            raise RuntimeError(f"{device} allocated 超过冻结显存上限")
        gpu[device] = {
            "allocated_gib": allocated,
            "reserved_gib": reserved,
            "free_gib": free / 1024**3,
            "total_gib": total / 1024**3,
        }
    return {
        "cpu_rss_gib": rss,
        "system_available_gib": psutil.virtual_memory().available / 1024**3,
        "cuda": gpu,
    }
