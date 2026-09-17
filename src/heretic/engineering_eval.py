# SPDX-License-Identifier: AGPL-3.0-or-later
"""Evaluate an exported ARA adapter against its base on identical prompts."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .engineering_direct import (
    MAX_SOURCE_ROWS,
    _build_protocol,
    _load_settings,
    _positive_integer,
)


def build_parser() -> argparse.ArgumentParser:
    """Return the standalone evaluation CLI; no GPU imports are required."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--config-template", type=Path, required=True)
    parser.add_argument("--eval-samples", type=_positive_integer, default=20)
    parser.add_argument("--start-index", default="auto")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--output-root", type=Path, default=Path("eval-runs"))
    parser.add_argument("--seed", type=int, choices=(42, 43, 44), default=42)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def resolve_data_range(adapter: Path, start: str, count: int) -> dict:
    """Resolve exact per-side rows and record known training overlap."""
    source = adapter.parent.parent / "engineering-config.json"
    training_stop = None
    if source.is_file():
        training = json.loads(source.read_text(encoding="utf-8"))
        training_stop = max(
            row["stop"] for row in training["per_side_data"].values()
        )
    if start == "auto":
        if training_stop is None:
            raise ValueError(
                "Cannot infer evaluation rows without engineering-config.json; "
                "set --start-index explicitly for this adapter."
            )
        first = training_stop
    else:
        first = int(start)
    if first < 0 or count < 1 or first + count > MAX_SOURCE_ROWS:
        raise ValueError(
            f"Require 0 <= start and start + eval-samples <= {MAX_SOURCE_ROWS}; "
            f"received start={first}, eval-samples={count}"
        )
    return {
        "start": first,
        "stop": first + count,
        "count_per_side": count,
        "training_stop": training_stop,
        "overlaps_training_run": (
            first < training_stop if training_stop is not None else None
        ),
        "training_config": str(source) if source.is_file() else None,
    }


def validate_adapter_config(adapter: Path) -> dict:
    """Require a local, unit-scale ARA LoRA export before allocating a GPU."""
    config = json.loads(
        (adapter / "adapter_config.json").read_text(encoding="utf-8")
    )
    if not (adapter / "adapter_model.safetensors").is_file():
        raise ValueError("Missing adapter_model.safetensors in adapter directory")
    rank = config.get("r")
    if (
        config.get("peft_type") != "LORA"
        or type(rank) is not int
        or rank < 1
        or config.get("lora_alpha") != rank
        or config.get("bias", "none") != "none"
        or any(config.get(key) for key in (
            "use_dora", "use_rslora", "fan_in_fan_out", "modules_to_save",
            "rank_pattern", "alpha_pattern", "lora_bias",
        ))
    ):
        raise ValueError("Expected an ARA LoRA export with unit scaling/no bias")
    return config


def load_exported_adapter(model, adapter: Path) -> dict:
    """Load all exported tensors into the default adapter used by ARA scorers."""
    from peft import get_peft_model_state_dict, set_peft_model_state_dict
    from safetensors.torch import load_file

    from .ara_research_schema import file_digest

    weights_path = adapter / "adapter_model.safetensors"
    weights = load_file(str(weights_path), device="cpu")
    expected = get_peft_model_state_dict(model.model, adapter_name="default")
    if weights.keys() != expected.keys():
        raise ValueError("Adapter tensor names do not match this model's targets")
    for name, tensor in weights.items():
        if tensor.shape != expected[name].shape:
            raise ValueError(f"Adapter tensor shape mismatch: {name}")
        if not tensor.isfinite().all():
            raise ValueError(f"Adapter tensor is not finite: {name}")
    set_peft_model_state_dict(model.model, weights, adapter_name="default")
    model.model.set_adapter("default")
    model.model.requires_grad_(False)
    model.model.eval()
    return {
        "path": str(adapter),
        "weights_sha256": file_digest(weights_path),
        "config_sha256": file_digest(adapter / "adapter_config.json"),
        "tensor_count": len(weights),
    }


def load_eval_prompts(settings, data_range: dict) -> dict:
    """Load exactly the requested slice from both pinned dataset revisions."""
    from .utils import load_prompts

    partitions = {}
    split = f"train[{data_range['start']}:{data_range['stop']}]"
    for side in ("good", "bad"):
        specification = getattr(settings, f"{side}_prompts").model_copy(
            update={"split": split}
        )
        prompts = load_prompts(settings, specification)
        if len(prompts) != data_range["count_per_side"]:
            raise ValueError(f"{side} dataset did not return the requested rows")
        partitions[side] = {"evaluation": prompts}
    return partitions


def summarize_scores(scores: dict) -> dict:
    """Compare keyword rates without equating this proxy to overall quality."""
    baseline, candidate = scores["baseline_keywords"], scores["keywords"]
    return {
        "baseline_keywords": baseline,
        "adapter_keywords": candidate,
        "reduction_percentage_points": (baseline - candidate) * 100,
        "relative_reduction": (
            (baseline - candidate) / baseline if baseline else None
        ),
        "effect_status": (
            "improved" if candidate < baseline
            else "unchanged" if candidate == baseline else "worsened"
        ),
        "first_token_kl": scores["first_token_kl"],
        "sequence_kl": scores["sequence_kl"],
        "within_kl_reference": (
            scores["first_token_kl"] <= 0.15
            and scores["sequence_kl"] <= 0.15
        ),
        "sample_count_per_side": scores["sample_count"],
    }


def attach_prompt_evidence(scores: dict, protocol: dict, prompts: dict) -> dict:
    """Attach exact model inputs, validating IDs and frozen prompt hashes."""
    from .ara_research_schema import digest

    inputs = {}
    for side in ("good", "bad"):
        identities = protocol["roles"][f"evaluation.{side}"]["prompts"]
        records = []
        for identity, prompt in zip(identities, prompts[side], strict=True):
            if digest([prompt.system, prompt.user]) != (
                identity["normalized_text_hash"]
            ):
                raise ValueError(f"Prompt content mismatch: {identity['prompt_id']}")
            records.append({
                "prompt_id": identity["prompt_id"],
                "question": prompt.user,
                "system_prompt": prompt.system,
            })
        if len({row["prompt_id"] for row in records}) != len(records):
            raise ValueError(f"Duplicate {side} prompt IDs")
        inputs[side] = records
    by_id = {row["prompt_id"]: row for row in inputs["bad"]}
    enriched = {**scores, "inputs": inputs}
    for key in ("responses", "baseline_responses"):
        responses = scores[key]
        ids = [row["prompt_id"] for row in responses]
        if len(ids) != len(by_id) or set(ids) != set(by_id):
            raise ValueError(f"Response/prompt IDs do not match: {key}")
        enriched[key] = [
            {**row, **by_id[row["prompt_id"]]} for row in responses
        ]
    return enriched


def prepare_options(options) -> tuple[Path, dict, dict]:
    """Validate inputs and describe a new output directory without writing it."""
    options.model = options.model.expanduser().resolve()
    options.adapter = options.adapter.expanduser().resolve()
    options.config_template = options.config_template.expanduser().resolve()
    if not (options.model / "config.json").is_file():
        raise ValueError(f"Local model config not found: {options.model}")
    if not options.config_template.is_file():
        raise ValueError(f"Template not found: {options.config_template}")
    config = validate_adapter_config(options.adapter)
    rows = resolve_data_range(
        options.adapter, options.start_index, options.eval_samples
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = options.output_dir or (
        options.output_root / f"ara-eval-{stamp}-{uuid4().hex[:6]}"
    )
    output = output.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"Evaluation output directory is not empty: {output}")
    return output, rows, config


def evaluate(options, output: Path, rows: dict, config: dict) -> dict:
    """Evaluate a frozen export and write full paired response evidence."""
    import torch

    from .ara_research_schema import write_json
    from .model import Model
    from .research_evaluation import RoleEvaluator

    if not torch.cuda.is_available():
        raise ValueError("A CUDA GPU is required for this evaluation template")
    settings = _load_settings(options, output)
    settings.ara_lora_rank = config["r"]
    partitions = load_eval_prompts(settings, rows)
    print("Loading base model and exported adapter...", flush=True)
    model = Model(settings)
    adapter = load_exported_adapter(model, options.adapter)
    protocol = _build_protocol(
        model, settings, partitions,
        {"evaluation": (rows["start"], rows["stop"])},
    )
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "eval-protocol.json", protocol, immutable=True)
    prompts = {side: value["evaluation"] for side, value in partitions.items()}
    print("Evaluating base model (adapter disabled)...", flush=True)
    evaluator = RoleEvaluator(model, protocol, "evaluation", prompts)
    print("Evaluating adapter on the same prompts...", flush=True)
    scores = evaluator(model)
    scores = attach_prompt_evidence(scores, protocol, prompts)
    write_json(output / "eval-evidence.json", scores, immutable=True)
    result = {
        "status": "completed", "mode": "engineering-evaluation",
        "model": str(options.model), "adapter": adapter,
        "data": rows, "seed": options.seed, "scores": summarize_scores(scores),
        "limitations": [
            "Keyword matching is a refusal proxy, not response-quality scoring.",
            "KL measures distribution drift, not general capability retention.",
            "This evaluation does not establish formal research acceptance.",
        ],
    }
    write_json(output / "eval-result.json", result, immutable=True)
    return result


def main(argv: list[str] | None = None) -> None:
    """Run the CLI and print the comparison or an actionable failure."""
    options = build_parser().parse_args(argv)
    try:
        output, rows, config = prepare_options(options)
        print(f"Adapter: {options.adapter}")
        print(f"Evaluation rows per side: [{rows['start']}:{rows['stop']}] "
              f"({rows['count_per_side']} good + {rows['count_per_side']} bad)")
        print(f"Overlap with source training run: {rows['overlaps_training_run']}")
        print(f"Output directory: {output}", flush=True)
        if options.dry_run:
            print("Status: dry-run")
            return
        result = evaluate(options, output, rows, config)
    except (ValueError, FileNotFoundError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    scores = result["scores"]
    print(f"Baseline keywords: {scores['baseline_keywords']:.2%}")
    print(f"Adapter keywords: {scores['adapter_keywords']:.2%}")
    print(f"Reduction: {scores['reduction_percentage_points']:.2f} pp")
    print(f"First-token KL: {scores['first_token_kl']:.6f}")
    print(f"Sequence KL: {scores['sequence_kl']:.6f}")
    print(f"Effect: {scores['effect_status']}")
    print(f"Result: {output / 'eval-result.json'}")
    print("Status: completed")


if __name__ == "__main__":
    main()
