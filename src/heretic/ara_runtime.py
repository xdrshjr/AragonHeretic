# SPDX-License-Identifier: AGPL-3.0-or-later
"""Configuration-driven runtime guards and trajectory model facade helpers."""

from __future__ import annotations

import hashlib
import importlib
import json
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

import huggingface_hub
import psutil
import lm_eval
import numpy as np
import questionary
import torch
from huggingface_hub import HfApi, ModelCard, ModelCardData
from lm_eval.models.huggingface import HFLM
from questionary import Choice, Style
from rich.table import Table

from .ara import TargetModule, get_lora_factors
from .ara_config import ARARuntimeGuard
from .continuation_scores import continuation_logprobs, validate_prefix_groups
from .ara_trajectory import (
    ContinuationRecord,
    TokenizedTrajectoryBatch,
    TrajectoryBoundaries,
    TrajectoryCaptureConfig,
    TrajectoryIO,
    TrajectoryRuntimeGuardError,
    capture_trajectory_io,
    causal_capture_positions,
    continuation_record,
    prompt_step_weights,
    valid_continuation_tokens,
)
from .system import empty_cache
from .utils import (
    Prompt,
    ask_if_unset,
    batchify,
    get_readme_intro,
    is_hf_path,
    upload_reproduce_folder,
)
from .utils import print as rich_print


@dataclass(frozen=True)
class RuntimeResources:
    cpu_rss_gib: float
    cuda_free_gib: Mapping[str, float]
    cuda_allocated_gib: Mapping[str, float]


@dataclass(frozen=True)
class RuntimeGuardReport:
    """Validated inventory, device set, and resource measurements."""

    target_total: int
    target_counts: Mapping[str, int]
    target_devices: tuple[str, ...]
    resources: RuntimeResources


@dataclass(frozen=True)
class UploadActionContext:
    settings: Any
    model: Any
    evaluator: Any
    study: Any
    trial: Any
    checkpoint_path: str
    reproduction: Mapping[str, Any] | None
    reset_model: Any


def manifest_fingerprint(manifest: Mapping[str, Any]) -> str:
    """Return the canonical SHA-256 identity of a model manifest."""
    encoded = json.dumps(
        manifest,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _base_target_name(name: str) -> str:
    return name.removeprefix("base_model.model.")


def _collect_runtime_resources(devices: Sequence[str]) -> RuntimeResources:
    rss = psutil.Process().memory_info().rss / 1024**3
    with suppress(ImportError):
        resource = importlib.import_module("resource")
        usage = getattr(resource, "getrusage")(getattr(resource, "RUSAGE_SELF"))
        peak = usage.ru_maxrss / 1024**2
        rss = max(rss, peak)
    free: dict[str, float] = {}
    allocated: dict[str, float] = {}
    for device in devices:
        if not device.startswith("cuda:"):
            continue
        index = int(device.partition(":")[2])
        free_bytes, _ = torch.cuda.mem_get_info(index)
        free[device] = free_bytes / 1024**3
        allocated[device] = torch.cuda.max_memory_allocated(index) / 1024**3
    return RuntimeResources(rss, free, allocated)


def _match_target(target: TargetModule, guard: ARARuntimeGuard) -> str:
    matches = [
        expected
        for expected in guard.targets
        if expected.name_pattern in target.full_name
    ]
    if len(matches) != 1:
        raise TrajectoryRuntimeGuardError(
            "device",
            f"target {target.full_name!r} matches {len(matches)} guard patterns",
        )
    expected = matches[0]
    shape = (target.out_features, target.in_features)
    if shape != (expected.out_features, expected.in_features):
        raise TrajectoryRuntimeGuardError(
            "device",
            f"target {target.full_name!r} shape {shape} violates runtime guard",
            target.key,
        )
    return expected.name_pattern


def validate_runtime_guard(
    targets: Sequence[TargetModule],
    guard: ARARuntimeGuard,
    resources: RuntimeResources | None = None,
) -> RuntimeGuardReport:
    """Validate target inventory, placement, and configured resource limits."""
    if len(targets) != guard.expected_target_total:
        raise TrajectoryRuntimeGuardError(
            "device", f"target total {len(targets)} != {guard.expected_target_total}"
        )
    counts = {target.name_pattern: 0 for target in guard.targets}
    devices = set()
    for target in targets:
        counts[_match_target(target, guard)] += 1
        devices.add(str(next(target.module.parameters()).device))
    expected_counts = {target.name_pattern: target.count for target in guard.targets}
    if counts != expected_counts:
        raise TrajectoryRuntimeGuardError(
            "device", f"target inventory {counts} does not match {expected_counts}"
        )
    required_devices = set(guard.required_target_devices)
    if devices != required_devices:
        raise TrajectoryRuntimeGuardError(
            "device",
            f"target devices {sorted(devices)} do not match {sorted(required_devices)}",
        )
    measured = resources or _collect_runtime_resources(sorted(devices))
    for device in required_devices:
        if measured.cuda_free_gib.get(device, 0) < guard.min_free_cuda_gib_after_load:
            raise TrajectoryRuntimeGuardError(
                "oom", f"{device} free memory is below the runtime guard"
            )
        if measured.cuda_allocated_gib.get(device, 0) > guard.max_cuda_allocated_gib:
            raise TrajectoryRuntimeGuardError(
                "oom", f"{device} allocation exceeds the runtime guard"
            )
    if measured.cpu_rss_gib > guard.max_capture_cpu_gib:
        raise TrajectoryRuntimeGuardError(
            "oom", "CPU RSS exceeds the trajectory capture guard"
        )
    return RuntimeGuardReport(
        target_total=len(targets),
        target_counts=counts,
        target_devices=tuple(sorted(devices)),
        resources=measured,
    )


def runtime_report_dict(report: RuntimeGuardReport) -> dict[str, Any]:
    """Serialize a runtime guard report for acceptance artifacts."""
    return asdict(report)


def run_chat_session(model: Any, system_prompt: str) -> None:
    """Run the interactive chat action for an adapted model."""
    rich_print()
    rich_print("[cyan]Press Ctrl+C at any time to return to the menu.[/]")
    chat = [{"role": "system", "content": system_prompt}]
    while True:
        try:
            message = questionary.text("User:", qmark=">").unsafe_ask()
            if not message:
                return
            chat.append({"role": "user", "content": message})
            rich_print("[bold]Assistant:[/] ", end="")
            response = model.stream_chat_response(chat)
            chat.append({"role": "assistant", "content": response})
        except (KeyboardInterrupt, EOFError):
            return


def _select_benchmarks(settings: Any) -> tuple[list[Any], bool] | None:
    benchmarks = questionary.checkbox(
        "Which benchmarks do you want to run?",
        [
            Choice(title=f"{item.name}: {item.description}", value=item)
            for item in settings.benchmarks
        ],
        style=Style([("highlighted", "reverse")]),
    ).ask()
    if not benchmarks:
        return None
    scope = questionary.select(
        "Benchmark the original model along with the decensored model?",
        choices=[
            "Benchmark only the decensored model",
            "Benchmark both models",
        ],
        style=Style([("highlighted", "reverse")]),
    ).ask()
    if scope is None:
        return None
    return benchmarks, scope == "Benchmark both models"


def _benchmark_table(compare_original: bool) -> Table:
    table = Table()
    table.add_column("Benchmark")
    table.add_column("Metric")
    if compare_original:
        table.add_column("This model", justify="right")
        table.add_column("Original model", justify="right")
    else:
        table.add_column("Value", justify="right")
    return table


def _format_metric(value: Any) -> str:
    if isinstance(value, (float, np.floating)):
        return f"{value:.4f}"
    return f"{value}"


def _evaluate_benchmark(hflm: HFLM, task: str) -> dict[str, Any]:
    results = lm_eval.simple_evaluate(model=hflm, tasks=[task])
    return results["results"][task]


def _add_benchmark_rows(
    table: Table,
    benchmark: Any,
    results: Mapping[str, Any],
    original: Mapping[str, Any] | None,
    separate: bool,
) -> None:
    first = True
    if separate:
        table.add_row(*([""] * (4 if original is not None else 3)))
    for metric, value in results.items():
        if metric == "alias":
            continue
        cells = [benchmark.name if first else "", metric, _format_metric(value)]
        if original is not None:
            cells.append(_format_metric(original[metric]))
        table.add_row(*cells)
        first = False


def run_benchmark_session(settings: Any, model: Any) -> None:
    """Run selected lm-eval tasks for the adapted and optional base model."""
    selected = _select_benchmarks(settings)
    if selected is None:
        return
    benchmarks, compare_original = selected
    hflm = HFLM(
        pretrained=model.model,  # ty:ignore[invalid-argument-type]
        tokenizer=model.tokenizer,  # ty:ignore[invalid-argument-type]
        batch_size="auto",
    )
    table = _benchmark_table(compare_original)
    try:
        for index, benchmark in enumerate(benchmarks):
            rich_print(f"Running benchmark [bold]{benchmark.name}[/]...")
            results = _evaluate_benchmark(hflm, benchmark.task)
            original = None
            if compare_original:
                with model.model.disable_adapter():  # ty:ignore[call-non-callable]
                    original = _evaluate_benchmark(hflm, benchmark.task)
            _add_benchmark_rows(table, benchmark, results, original, index > 0)
    except KeyboardInterrupt:
        pass
    if table.rows:
        rich_print(table)


def _upload_identity(settings: Any) -> tuple[str, Mapping[str, Any]] | None:
    token = huggingface_hub.get_token()
    if not token:
        token = questionary.password("Hugging Face access token:").ask()
    if not token:
        return None
    user = huggingface_hub.whoami(token)
    fullname = user.get("fullname", user.get("name", "unknown user"))
    rich_print(
        f"Logged in as [bold]{fullname} ({user.get('email', 'no email found')})[/]"
    )
    return token, user


def _upload_destination(
    context: UploadActionContext,
    user: Mapping[str, Any],
) -> tuple[str, bool] | None:
    settings = context.settings
    repo_id = ask_if_unset(
        settings.upload_repo_id,
        questionary.text(
            "Name of repository:",
            default=f"{user['name']}/{Path(settings.model).name}-heretic",
        ),
    )
    if not repo_id:
        return None
    configured = settings.upload_repo_private
    value = None if configured is None else ("Private" if configured else "Public")
    visibility = ask_if_unset(
        value,
        questionary.select(
            "Should the repository be public or private?",
            choices=["Public", "Private"],
            style=Style([("highlighted", "reverse")]),
        ),
    )
    if visibility is None:
        return None
    return repo_id, visibility == "Private"


def _is_reproducible_upload(context: UploadActionContext) -> bool:
    settings, evaluator = context.settings, context.evaluator
    datasets = [
        settings.good_prompts,
        settings.bad_prompts,
        *evaluator.get_dataset_specifications(),
    ]
    return (
        is_hf_path(settings.model)
        and all(
            is_hf_path(item.dataset) and item.commit is not None for item in datasets
        )
        and evaluator.all_scorers_reproducible()
        and evaluator.all_scorers_builtin()
        and context.reproduction is None
    )


def _reproduction_level(context: UploadActionContext) -> str | None:
    if not _is_reproducible_upload(context):
        return "none"
    choices = [
        Choice("Full: Settings, packages, and system information", value="full"),
        Choice("Basic: Settings and package versions", value="basic"),
        Choice("Don't add reproducibility information", value="none"),
    ]
    return ask_if_unset(
        context.settings.upload_reproducibility_information,
        questionary.select(
            "Which reproducibility information do you want to add?",
            choices=choices,
            style=Style([("highlighted", "reverse")]),
        ),
    )


def _push_model(
    context: UploadActionContext,
    destination: tuple[str, bool],
    token: str,
    strategy: Any,
) -> None:
    repo_id, private = destination
    kwargs = {
        "private": private,
        "max_shard_size": context.settings.max_shard_size,
        "token": token,
    }
    if strategy.value == "adapter":
        rich_print("Uploading LoRA adapter...")
        context.model.model.push_to_hub(repo_id, **kwargs)
        return
    rich_print("Uploading merged model...")
    merged = context.model.get_merged_model()
    merged.push_to_hub(repo_id, **kwargs)
    del merged
    empty_cache()
    context.model.tokenizer.push_to_hub(repo_id, private=private, token=token)
    if context.model.processor is not None:
        context.model.processor.push_to_hub(repo_id, private=private, token=token)
    context.reset_model()


def _base_model_card(settings: Any) -> ModelCard | None:
    if is_hf_path(settings.model):
        return ModelCard.load(settings.model)
    card_path = Path(settings.model) / huggingface_hub.constants.REPOCARD_NAME
    return ModelCard.load(card_path) if card_path.exists() else None


def _push_model_card(
    context: UploadActionContext, repo_id: str, token: str, level: str
) -> None:
    card = _base_model_card(context.settings)
    if card is None:
        return
    if card.data is None:
        card.data = ModelCardData()
    data = cast(Any, card.data)
    data.tags = list(data.tags or [])
    data.tags.extend(["heretic", "uncensored", "decensored", "abliterated"])
    if level != "none":
        data.tags.append("reproducible")
    card.text = (
        get_readme_intro(context.settings, context.trial, level != "none") + card.text
    )
    card.push_to_hub(repo_id, token=token)


def _upload_reproduction(
    context: UploadActionContext,
    repo_id: str,
    token: str,
    level: str,
    strategy: Any,
) -> None:
    if level == "none":
        return
    settings = context.settings
    settings.n_trials = len(context.study.trials)
    previous = settings.export_strategy
    settings.export_strategy = strategy
    try:
        upload_reproduce_folder(
            repo_id,
            settings,
            token,
            checkpoint_path=context.checkpoint_path,
            trial=context.trial,
            include_system_information=level == "full",
        )
    finally:
        settings.export_strategy = previous


def _verify_remote_reproduction(
    repo_id: str,
    token: str,
    reproduction: Mapping[str, Any] | None,
) -> None:
    if reproduction is None:
        return
    info = HfApi().model_info(repo_id, files_metadata=True, token=token)
    files = {item.rfilename: item for item in info.siblings or ()}
    for filename, expected in reproduction["hashes"].items():
        item = files.get(filename)
        sha256 = getattr(item, "lfs", {}).get("sha256") if item else None
        if not sha256:
            raise RuntimeError(f"uploaded model is missing hash for {filename}")
        if sha256.lower() != expected.lower():
            raise RuntimeError(f"uploaded file hash mismatch: {filename}")
        rich_print(f"[bold]{filename}:[/] [green]Hash matches[/]")


def run_upload_action(context: UploadActionContext) -> None:
    """Upload an ungated model and optional reproducibility bundle."""
    from .workflow import obtain_export_strategy

    if context.settings.acceptance_gate is not None:
        raise ValueError("CARA gate requires verified local staging")
    identity = _upload_identity(context.settings)
    if identity is None:
        return
    token, user = identity
    destination = _upload_destination(context, user)
    if destination is None:
        return
    strategy = obtain_export_strategy(context.settings, context.model)
    if strategy is None:
        return
    level = _reproduction_level(context)
    if level is None:
        return
    repo_id, _ = destination
    _push_model(context, destination, token, strategy)
    _push_model_card(context, repo_id, token, level)
    _upload_reproduction(context, repo_id, token, level, strategy)
    rich_print(f"Model uploaded to [bold]{repo_id}[/].")
    _verify_remote_reproduction(repo_id, token, context.reproduction)


def capture_adapter_factor_state(
    targets: Sequence[TargetModule],
) -> dict[str, torch.Tensor]:
    """Copy all active default A/B factors to CPU under stable names."""
    state = {}
    for target in targets:
        lora_a, lora_b = get_lora_factors(target)
        state[f"{target.full_name}.lora_A"] = lora_a.detach().cpu().clone()
        state[f"{target.full_name}.lora_B"] = lora_b.detach().cpu().clone()
    return state


def capture_named_adapter_state(
    model: Any, adapter_name: str
) -> dict[str, torch.Tensor]:
    """Capture one PEFT adapter with its name segment normalized away."""
    state = {}
    markers = (f".lora_A.{adapter_name}.weight", f".lora_B.{adapter_name}.weight")
    for name, parameter in model.named_parameters():
        marker = next((value for value in markers if name.endswith(value)), None)
        if marker is None:
            continue
        normalized = name.removesuffix(marker) + marker.replace(
            f".{adapter_name}.weight", ""
        )
        state[normalized] = parameter.detach().cpu().to(torch.float32).clone()
    if not state:
        raise RuntimeError(f"adapter {adapter_name!r} has no LoRA factor tensors")
    return state


def adapter_state_identity(state: Mapping[str, torch.Tensor]) -> str:
    """Hash shared v2/v3 named FP32 factors without serializing them."""
    digest = hashlib.sha256()
    for name in sorted(state):
        tensor = state[name].detach().cpu().to(torch.float32).contiguous()
        digest.update(name.encode())
        digest.update(json.dumps(list(tensor.shape)).encode())
        digest.update(tensor.numpy().tobytes())
    return digest.hexdigest()


def create_model_manifest(model: Any) -> dict[str, Any]:
    """Build the stable base-model, target, and template identity manifest."""
    config = model.model.config
    targets = [
        {
            "name": _base_target_name(target.full_name),
            "shape": [target.out_features, target.in_features],
        }
        for target in model.get_target_modules()
    ]
    template = str(model.tokenizer.chat_template or "")
    return {
        "model": model.settings.model,
        "requested_revision": model.settings.model_commit,
        "commit": getattr(config, "_commit_hash", None),
        "model_type": getattr(config, "model_type", None),
        "layers": len(model.get_layers()),
        "targets": targets,
        "chat_template_sha256": hashlib.sha256(template.encode()).hexdigest(),
    }


def verify_reloaded_base(model: Any, base_model: Any) -> None:
    """Reject a reload whose target graph differs from the initial model."""
    modules = dict(base_model.named_modules())
    targets = []
    for expected in model.model_manifest["targets"]:
        module = modules.get(expected["name"])
        weight = getattr(module, "weight", None)
        if weight is None or len(weight.shape) != 2:
            raise RuntimeError(f"merged base has no target {expected['name']}")
        targets.append({"name": expected["name"], "shape": list(weight.shape)})
    try:
        layers = base_model.model.language_model.layers
    except AttributeError:
        layers = base_model.model.layers
    config = base_model.config
    manifest = {
        **model.model_manifest,
        "commit": getattr(config, "_commit_hash", None),
        "model_type": getattr(config, "model_type", None),
        "layers": len(layers),
        "targets": targets,
    }
    if manifest_fingerprint(manifest) != model.model_fingerprint:
        raise RuntimeError("merged base model fingerprint does not match loaded model")


def verify_model_fingerprint(model: Any) -> None:
    """Initialize or compare the stable loaded model fingerprint."""
    manifest = create_model_manifest(model)
    fingerprint = manifest_fingerprint(manifest)
    previous = getattr(model, "model_fingerprint", None)
    if previous is not None and previous != fingerprint:
        raise RuntimeError("model fingerprint changed during reload")
    model.model_manifest = manifest
    model.model_fingerprint = fingerprint


def render_prompt_texts(model: Any, prompts: list[Prompt]) -> list[str]:
    """Render prompts exactly as generation does, including response prefix."""
    chats = [
        [
            {"role": "system", "content": prompt.system},
            {"role": "user", "content": prompt.user},
        ]
        for prompt in prompts
    ]
    rendered = model._render_chat_template(chats)
    values = [rendered] if isinstance(rendered, str) else rendered
    if model.settings.response_prefix:
        values = [value + model.settings.response_prefix for value in values]
    return values


def continuation_cache_identity(
    model: Any,
    continuations: tuple[str, ...],
) -> tuple[tuple[tuple[int, ...], ...], str]:
    """Return tokenized prefixes and a stable tokenizer identity."""
    token_ids = []
    for continuation in continuations:
        encoded = model.tokenizer(
            continuation.strip(),
            add_special_tokens=False,
            return_attention_mask=False,
            return_token_type_ids=False,
        )["input_ids"]
        token_ids.append(tuple(int(value) for value in encoded))
    identity = (
        f"{type(model.tokenizer).__module__}.{type(model.tokenizer).__name__}:"
        f"{getattr(model.tokenizer, 'name_or_path', '')}:{len(model.tokenizer)}"
    )
    return tuple(token_ids), identity


def score_model_continuations(
    model: Any,
    prompts: list[Prompt],
    continuations: tuple[str, ...],
    batch_tokens: int,
) -> torch.Tensor:
    """Score response prefixes through the shared rendering/model facade."""
    return continuation_logprobs(
        model.model,
        model.tokenizer,
        render_prompt_texts(model, prompts),
        continuations,
        batch_tokens,
    )


def validate_model_prefix_groups(
    model: Any,
    refusal_prefixes: list[str],
    answer_prefixes: list[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Validate scorer prefixes against the loaded tokenizer."""
    return validate_prefix_groups(model.tokenizer, refusal_prefixes, answer_prefixes)


def _teacher_inputs(
    tokenizer: Any,
    prompt_ids: Sequence[Sequence[int]],
    continuations: Sequence[Sequence[int]],
) -> tuple[torch.Tensor, torch.Tensor]:
    rows = [
        list(prompt) + list(continuation[:-1])
        for prompt, continuation in zip(prompt_ids, continuations)
    ]
    width = max(len(row) for row in rows)
    input_ids = []
    masks = []
    for row in rows:
        padding = width - len(row)
        input_ids.append([tokenizer.pad_token_id] * padding + row)
        masks.append([0] * padding + [1] * len(row))
    return (
        torch.tensor(input_ids, dtype=torch.long),
        torch.tensor(masks, dtype=torch.long),
    )


def _generated_sequences(outputs: Any) -> torch.Tensor:
    sequences = getattr(outputs, "sequences", outputs)
    if not isinstance(sequences, torch.Tensor) or sequences.ndim != 2:
        raise RuntimeError("trajectory generation did not return token sequences")
    return sequences


def _generate_trajectory_tokens(
    model: Any,
    prompts: list[Prompt],
) -> tuple[list[list[int]], list[tuple[int, ...]]]:
    settings = model.settings
    inputs, generated = model.generate(
        prompts,
        max_new_tokens=settings.ara_trajectory_tokens,
        do_sample=False,
        use_cache=True,
    )
    sequences = _generated_sequences(generated)
    input_ids = cast(torch.Tensor, inputs["input_ids"])
    attention_mask = cast(torch.Tensor, inputs["attention_mask"])
    prompt_ids: list[list[int]] = [  # ty:ignore[invalid-assignment]
        input_ids[row][attention_mask[row].bool()].detach().cpu().tolist()
        for row in range(input_ids.shape[0])
    ]
    start = input_ids.shape[1]
    continuations = [
        valid_continuation_tokens(
            sequences[row, start:].detach().cpu().tolist(),
            model.tokenizer.eos_token_id,
            settings.ara_trajectory_tokens,
        )
        for row in range(sequences.shape[0])
    ]
    return prompt_ids, continuations


def _trajectory_batch(
    model: Any,
    prompts: list[Prompt],
    prompt_offset: int,
) -> tuple[TokenizedTrajectoryBatch, TrajectoryBoundaries, list[ContinuationRecord]]:
    settings = model.settings
    prompt_token_ids, continuations = _generate_trajectory_tokens(model, prompts)
    teacher_ids, teacher_mask = _teacher_inputs(
        model.tokenizer,
        prompt_token_ids,
        continuations,
    )
    lengths = [len(value) for value in continuations]
    prompt_indices, step_indices, weights = prompt_step_weights(
        lengths,
        settings.ara_trajectory_decay,
    )
    prompt_indices += prompt_offset
    positions = causal_capture_positions(
        teacher_mask,
        [len(value) for value in prompt_token_ids],
        lengths,
    )
    device = model.model.device

    def forward() -> Any:
        with torch.inference_mode():
            return model.model(
                input_ids=teacher_ids.to(device),
                attention_mask=teacher_mask.to(device),
                use_cache=False,
            )

    records = [
        continuation_record(value, model.tokenizer.eos_token_id)
        for value in continuations
    ]
    boundary = TrajectoryBoundaries(
        positions=positions,
        prompt_indices=prompt_indices,
        step_indices=step_indices,
        weights=weights,
    )
    return TokenizedTrajectoryBatch(forward), boundary, records


def capture_model_trajectory(
    model: Any,
    prompts: list[Prompt],
) -> tuple[TrajectoryIO, tuple[ContinuationRecord, ...]]:
    """Generate base continuations and capture one teacher-forced pass per batch."""
    batches = []
    boundaries = []
    records = []
    offset = 0
    for prompts_batch in batchify(prompts, model.settings.ara_capture_batch_size):
        batch, boundary, batch_records = _trajectory_batch(
            model,
            prompts_batch,
            offset,
        )
        batches.append(batch)
        boundaries.append(boundary)
        records.extend(batch_records)
        offset += len(prompts_batch)
    captured = capture_trajectory_io(
        model.ara_targets,
        batches,
        boundaries,
        TrajectoryCaptureConfig(model.settings.ara_trajectory_tokens),
    )
    return captured, tuple(records)
