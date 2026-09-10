# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026  Philipp Emanuel Weidmann <pew@worldwidemann.com> + contributors

import json
import os
import platform
import tempfile
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Any, Mapping, TypeVar

import huggingface_hub
import tomli_w
import torch
from datasets import DatasetDict, ReadInstruction, load_dataset, load_from_disk
from datasets.config import DATASET_STATE_JSON_FILENAME
from datasets.download.download_manager import DownloadMode
from datasets.utils.info_utils import VerificationMode
from huggingface_hub.utils import validate_repo_id
from optuna import Trial
from optuna.study import StudyDirection
from optuna.trial import FrozenTrial
from psutil import Process
from questionary import Question
from rich.console import Console

from .artifact_schema import (
    acceptance_binding,
    file_sha256 as _file_sha256,
    generate_sha256sums as _generate_sha256sums,
)
from .ara_search import format_trial_parameters as get_trial_parameters
from .config import DatasetSpecification, Settings
from .system import (
    get_accelerator_info_dict,
    get_cpu_info_dict,
    get_heretic_version_info,
    get_python_env_info_dict,
    get_requirements_dict,
    is_xpu_available,
)

T = TypeVar("T")
generate_sha256sums = _generate_sha256sums
get_file_sha256 = _file_sha256


print = Console(highlight=False).print


def deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """
    Recursively merge two dicts.

    Values from `override` take precedence. Nested dicts are merged recursively.
    """
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_dicts(merged[key], value)  # type: ignore[arg-type]
        else:
            merged[key] = value
    return merged


def parse_study_direction(optimization: str) -> StudyDirection:
    """
    Converts the optimization value stored as a `str` to the
    `StudyDirection` object required by Optuna.
    """
    if optimization == "none":
        return StudyDirection.NOT_SET
    return StudyDirection[optimization.upper()]


def print_memory_usage():
    def p(label: str, size_in_bytes: int):
        print(f"[grey50]{label}: [bold]{size_in_bytes / (1024**3):.2f} GB[/][/]")

    p("Resident system RAM", Process().memory_info().rss)

    if torch.cuda.is_available():
        count = torch.cuda.device_count()
        allocated = sum(torch.cuda.memory_allocated(device) for device in range(count))
        reserved = sum(torch.cuda.memory_reserved(device) for device in range(count))
        p("Allocated GPU VRAM", allocated)
        p("Reserved GPU VRAM", reserved)
    elif is_xpu_available():
        count = torch.xpu.device_count()
        allocated = sum(torch.xpu.memory_allocated(device) for device in range(count))
        reserved = sum(torch.xpu.memory_reserved(device) for device in range(count))
        p("Allocated XPU memory", allocated)
        p("Reserved XPU memory", reserved)
    elif torch.backends.mps.is_available():
        p("Allocated MPS memory", torch.mps.current_allocated_memory())
        p("Driver (reserved) MPS memory", torch.mps.driver_allocated_memory())


def format_duration(seconds: float) -> str:
    seconds = round(seconds)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    if hours > 0:
        return f"{hours}h {minutes}m"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    else:
        return f"{seconds}s"


def format_exception(error: Exception) -> str:
    # Walk causal chain to find a non-empty message.
    current = error
    while current is not None:
        message = str(current).strip()
        if message:
            return message
        current = current.__cause__ or current.__context__

    # If there is no message in the entire causal chain, fall back to the complete traceback.
    return traceback.format_exc().strip()


def ask_if_unset(value: T, question: Question, unsafe: bool = False) -> T:
    if value is None:
        if unsafe:
            return question.unsafe_ask()
        else:
            return question.ask()
    else:
        return value


def is_hf_path(path: str) -> bool:
    """Checks whether a path likely refers to a Hugging Face repository."""

    # Match Transformers: Existing local paths take precedence over Hub lookup,
    # even if the path string is also a valid repository ID.
    if Path(path).exists():
        return False

    validate_repo_id(path)
    return True


@dataclass
class Prompt:
    system: str
    user: str


def get_split_slice(split_str: str, length: int) -> tuple[int, int]:
    """Resolves a split specification into absolute (start, end) indices."""

    # The split name is the part before the slice, e.g. "train" in "train[:400]".
    split_name = split_str.split("[")[0]

    # Associate the split with its number of examples (lines).
    name_to_length = {split_name: length}

    # Convert the instructions to absolute indices and select the first one.
    absolute_instruction = ReadInstruction.from_spec(split_str).to_absolute(
        name_to_length
    )[0]

    return absolute_instruction.from_, absolute_instruction.to


def _load_text_prompts(specification: DatasetSpecification) -> list[str]:
    with open(specification.dataset, encoding="utf-8") as file:
        prompts = [line.strip() for line in file if line.strip()]
    if specification.split is not None:
        start, end = get_split_slice(f"_{specification.split}", len(prompts))
        prompts = prompts[start:end]
    return prompts


def _load_prompt_dataset(specification: DatasetSpecification) -> list[str]:
    path, split = specification.dataset, specification.split
    if split is None:
        raise ValueError(f'The "split" field is required for datasets: {path}')
    if specification.column is None:
        raise ValueError(f'The "column" field is required for datasets: {path}')
    if is_hf_path(path):
        if specification.commit is None:
            try:
                specification.commit = huggingface_hub.dataset_info(path).sha
            except Exception as error:
                print(f"[yellow]Warning: dataset revision was not pinned ({error}).[/]")
        dataset = load_dataset(path, revision=specification.commit, split=split)
    elif Path(path, DATASET_STATE_JSON_FILENAME).exists():
        dataset = load_from_disk(path)
        assert not isinstance(dataset, DatasetDict), "Dataset dicts are unsupported"
        start, end = get_split_slice(split, len(dataset))
        dataset = dataset[start:end]
    else:
        dataset = load_dataset(
            path,
            split=split,
            verification_mode=VerificationMode.NO_CHECKS,
            download_mode=DownloadMode.FORCE_REDOWNLOAD,
        )
    return list(dataset[specification.column])


def _format_prompts(
    settings: Settings,
    specification: DatasetSpecification,
    prompts: list[str],
) -> list[Prompt]:
    if specification.prefix:
        prompts = [f"{specification.prefix} {prompt}" for prompt in prompts]
    if specification.suffix:
        prompts = [f"{prompt} {specification.suffix}" for prompt in prompts]
    system = settings.system_prompt
    if specification.system_prompt is not None:
        system = specification.system_prompt
    return [Prompt(system=system, user=prompt) for prompt in prompts]


def load_prompts(
    settings: Settings,
    specification: DatasetSpecification,
) -> list[Prompt]:
    """Load and render prompts from text, disk datasets, or the Hub."""
    if os.path.isfile(specification.dataset):
        prompts = _load_text_prompts(specification)
    else:
        prompts = _load_prompt_dataset(specification)
    return _format_prompts(settings, specification, prompts)


def batchify(items: list[T], batch_size: int) -> list[list[T]]:
    return [items[i : i + batch_size] for i in range(0, len(items), batch_size)]


def _score_rows(trial: Trial | FrozenTrial) -> str:
    return "\n".join(
        f"| **{score['name']}** | {score['score']['md_display']} | "
        f"{score['baseline']['md_display']} |"
        for score in trial.user_attrs["scores"]
    )


def _parameter_rows(trial: Trial | FrozenTrial) -> str:
    return "\n".join(
        f"| **{name}** | {value} |"
        for name, value in get_trial_parameters(trial).items()
    )


def _reproduction_tip(settings: Settings) -> str:
    target = (
        "See [`reproduce.json`](reproduce.json) for the verified identity envelope."
        if settings.ara_objective_version == "trajectory-v2"
        else "See the [README](reproduce/README.md) in the `reproduce` directory."
    )
    return f"""
> [!TIP]
> **This model is reproducible!**
>
> {target}
"""


def get_readme_intro(
    settings: Settings,
    trial: Trial | FrozenTrial,
    contains_reproducibility_information: bool,
) -> str:
    model_link = (
        f"[{settings.model}](https://huggingface.co/{settings.model})"
        if is_hf_path(settings.model)
        else "a model"
    )
    reproducibility = (
        _reproduction_tip(settings) if contains_reproducibility_information else ""
    )
    method = trial.user_attrs.get("method", "directional")
    description = (
        f"Calibrated Arbitrary-Rank Ablation (CARA-LoRA), rank {settings.ara_lora_rank}"
        if method == "ara"
        else "directional ablation"
    )
    gate_status = trial.user_attrs.get("acceptance_status", "not evaluated")
    return f"""# This is a decensored version of {model_link}, made using [Heretic](https://heretic-project.org) v{version("heretic-llm")}
{reproducibility}
## Method

- **Method:** {description}
- **Acceptance gate:** {gate_status}

## Abliteration parameters

| Parameter | Value |
| :-------- | :---: |
{_parameter_rows(trial)}

## Performance

| Metric | This model | Original model ({model_link}) |
| :----- | :--------: | :---------------------------: |
{_score_rows(trial)}

-----

"""


def generate_config_toml(settings: Settings) -> str:
    """Serializes the full Settings object to TOML."""

    return tomli_w.dumps(settings.model_dump(exclude_none=True))


def generate_requirements_txt() -> str:
    """Collects direct project dependencies as a formatted string."""

    requirements = [
        f"{package}=={version}" for package, version in get_requirements_dict().items()
    ]
    return "\n".join(requirements) + "\n"


def format_hf_link(
    path: str,
    commit: str | None = None,
    is_dataset: bool = False,
) -> str:
    prefix = "datasets/" if is_dataset else ""
    base_url = f"https://huggingface.co/{prefix}{path}"
    link = f"[{path}]({base_url})"

    if commit:
        commit_url = f"{base_url}/commit/{commit}"
        link += f" (Commit: [`{commit[:7]}`]({commit_url}))"

    return link


_REPRODUCTION_GUIDE = """# Reproduction guide

This directory contains the assets required to reproduce this Heretic run.
{heterogeneous_warning}{origin_warning}
## Models

- **Base model:** {model_link}

## Datasets

- **Good prompts:** {good_link}
- **Bad prompts:** {bad_link}

## Selected trial

- **Trial number:** {trial_index}
{score_lines}

{system_report}## Environment

- **Heretic:** v{heretic_version}{origin_suffix}
- **PyTorch:** {pytorch_version}
- **Other dependencies:** See [`requirements.txt`](requirements.txt).

## Contents

- [`requirements.txt`](requirements.txt): exact package versions.
- [`config.toml`](config.toml): effective configuration and RNG seed.
- [`{checkpoint_filename}`]({checkpoint_filename}): Optuna study journal.
- [`SHA256SUMS`](SHA256SUMS): weight-file hashes.
- [`reproduce.json`](reproduce.json): machine-readable identities.

## How to reproduce

1. {system_instruction}Install the Heretic and PyTorch versions listed above.
2. Install `requirements.txt`, place `config.toml` in the working directory, and run `heretic`.
3. Select trial **{trial_index}** and compare all outputs with `SHA256SUMS`.

The journal `{checkpoint_filename}` can resume the same study without re-running stored trials.
"""


def _accelerator_report() -> str:
    accelerators = get_accelerator_info_dict()
    if accelerators["type"] is None:
        return "**No GPU or other accelerator detected.**"
    devices = accelerators["devices"]
    total = sum(device.get("vram_gb", 0) for device in devices)
    lines = [
        f"- **{accelerators['type']}:** {len(devices)} device(s), {total:.2f} GB VRAM"
    ]
    if accelerators.get("api_name") and accelerators.get("api_version"):
        lines.append(
            f"  - **{accelerators['api_name']}:** {accelerators['api_version']}"
        )
    if accelerators.get("driver_version"):
        lines.append(f"  - **Driver Version:** {accelerators['driver_version']}")
    lines.extend(
        f"  - **Device {index}:** {device['name']}"
        for index, device in enumerate(devices)
    )
    return "\n".join(lines)


def _system_guide(include: bool) -> tuple[str, str, str]:
    if not include:
        return "", "", ""
    warning = ""
    if torch.cuda.is_available() and torch.cuda.device_count() > 1:
        names = {
            torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())
        }
        if len(names) > 1:
            warning = "\n> [!WARNING]\n> Heterogeneous GPUs can prevent exact reproducibility.\n"
    cpu, python = get_cpu_info_dict(), get_python_env_info_dict()
    report = f"""## System

- **Python:** {python["version"]} ({python["implementation"]})
- **Operating system:** {platform.platform()} ({platform.machine()})
- **CPU:** {cpu["brand"] or "Unknown"}

### Accelerators

{_accelerator_report()}

"""
    return warning, report, "Ensure the system matches the recorded system. "


def _origin_warning(version_info: Any) -> str:
    origin = version_info.origin
    if version_info.is_standard_pypi or not origin:
        return ""
    if origin.startswith("Git"):
        return (
            f"\n> [!IMPORTANT]\n> Install Heretic from the recorded source: {origin}.\n"
        )
    if origin == "Local":
        return "\n> [!WARNING]\n> Local source was used; exact reproduction is not guaranteed.\n"
    return "\n> [!WARNING]\n> A non-standard Heretic installation was used.\n"


def generate_reproduce_readme(
    settings: Settings,
    checkpoint_filename: str,
    trial: Trial | FrozenTrial,
    include_system_information: bool,
) -> str:
    """Generate the human-readable point-v1 reproduction guide."""
    version_info = get_heretic_version_info()
    warning, system_report, instruction = _system_guide(include_system_information)
    score_lines = "\n".join(
        f"- **{item['name']}:** {item['score']['md_display']} "
        f"(baseline: {item['baseline']['md_display']})"
        for item in trial.user_attrs["scores"]
    )
    return _REPRODUCTION_GUIDE.format(
        heterogeneous_warning=warning,
        origin_warning=_origin_warning(version_info),
        model_link=format_hf_link(settings.model, settings.model_commit),
        good_link=format_hf_link(
            settings.good_prompts.dataset, settings.good_prompts.commit, True
        ),
        bad_link=format_hf_link(
            settings.bad_prompts.dataset, settings.bad_prompts.commit, True
        ),
        trial_index=trial.user_attrs["index"],
        score_lines=score_lines,
        system_report=system_report,
        heretic_version=version_info.version,
        origin_suffix=f" (Origin: {version_info.origin})"
        if version_info.origin
        else "",
        pytorch_version=torch.__version__,
        checkpoint_filename=checkpoint_filename,
        system_instruction=instruction,
    )


def _reproduce_base_data(
    settings: Settings,
    trial: Trial | FrozenTrial,
    timestamp: str,
    hashes: dict[str, str],
) -> dict[str, Any]:
    from .trial_methods import parameter_envelope, parameters_from_trial

    version_info = get_heretic_version_info()
    return {
        "version": "4",
        "timestamp": timestamp,
        "system": None,
        "environment": {
            "heretic": {
                "version": version_info.version,
                "is_standard_pypi": version_info.is_standard_pypi,
                "metadata": version_info.metadata,
            },
            "pytorch_version": torch.__version__,
            "requirements": get_requirements_dict(),
        },
        "settings": settings.model_dump(),
        "parameters": parameter_envelope(parameters_from_trial(trial)),
        "scores": trial.user_attrs["scores"],
        "hashes": hashes,
        "model_fingerprint": trial.user_attrs.get("model_fingerprint"),
        "study_fingerprint": trial.user_attrs.get("study_fingerprint"),
        "calibration_fingerprint": trial.user_attrs.get("calibration_fingerprint"),
        "acceptance": _acceptance_binding(settings),
    }


def _add_trajectory_identity(
    data: dict[str, Any], settings: Settings, trial: Trial | FrozenTrial
) -> None:
    from .ara_search import SAMPLER_PROTOCOL, SEARCH_SPACE_VERSION
    from .artifact_schema import REPRODUCE_SCHEMA, canonical_sha256, ensure_finite_json

    acceptance = data["acceptance"]
    if not isinstance(acceptance, dict) or acceptance.get("status") != "passed":
        raise ValueError("trajectory-v2 reproduction requires passed acceptance")
    tables = (settings.model_extra or {}).get("scorer", {})
    refusal = tables.get("RefusalLogOdds", {}) if isinstance(tables, dict) else {}
    prefix_hash = canonical_sha256(
        {
            "refusal_prefixes": refusal.get("refusal_prefixes"),
            "answer_prefixes": refusal.get("answer_prefixes"),
        }
    )
    data.update(
        {
            "schema": REPRODUCE_SCHEMA,
            "objective_version": "trajectory-v2",
            "trajectory_manifest_sha256": trial.user_attrs.get(
                "trajectory_fingerprint"
            ),
            "prefix_set_sha256": prefix_hash,
            "search_space_version": SEARCH_SPACE_VERSION,
            "sampler_protocol": SAMPLER_PROTOCOL,
            "core_artifact_hashes": data["hashes"],
            "acceptance_sha256": acceptance["sha256"],
        }
    )
    ensure_finite_json(data)


def generate_reproduce_json(
    settings: Settings,
    trial: Trial | FrozenTrial,
    timestamp: str,
    uploaded_model_hashes: dict[str, str],
    include_system_information: bool,
) -> str:
    """Generate the machine-readable reproduction identity document."""
    if settings.ara_objective_version == "sequential-v3":
        from .ara_research_acceptance import verify_research_artifact_graph
        root = Path(settings.save_directory or "adapter")
        verify_research_artifact_graph(root)
        return (root / "reproduce.json").read_text(encoding="utf-8")
    data = _reproduce_base_data(settings, trial, timestamp, uploaded_model_hashes)
    if settings.ara_objective_version == "trajectory-v2":
        _add_trajectory_identity(data, settings, trial)
    if include_system_information:
        data["system"] = {
            "python": get_python_env_info_dict(),
            "os": {"platform": platform.platform(), "machine": platform.machine()},
            "cpu": get_cpu_info_dict(),
            "accelerators": get_accelerator_info_dict(),
        }
    else:
        del data["system"]
    return json.dumps(data, indent=4)


def _acceptance_binding(settings: Settings) -> dict[str, Any] | None:
    if settings.acceptance_gate is None:
        return None
    return acceptance_binding(settings.acceptance_gate.report_path)


def _write_reproduction_files(
    directory: Path,
    settings: Settings,
    checkpoint_name: str,
    trial: Trial | FrozenTrial,
    options: Mapping[str, Any],
) -> None:
    hashes = options["uploaded_model_hashes"]
    include = bool(options["include_system_information"])
    timestamp = (
        datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()
    )
    files = {
        "requirements.txt": generate_requirements_txt(),
        "config.toml": generate_config_toml(settings),
        "reproduce.json": generate_reproduce_json(
            settings, trial, timestamp, hashes, include
        ),
        "README.md": generate_reproduce_readme(
            settings, checkpoint_name, trial, include
        ),
    }
    if hashes:
        files["SHA256SUMS"] = generate_sha256sums(hashes)
    for name, content in files.items():
        (directory / name).write_text(content, encoding="utf-8")


def create_reproduce_folder(
    path: Path,
    settings: Settings,
    checkpoint_path: str | Path,
    trial: Trial | FrozenTrial,
    **options: Any,
) -> None:
    """Create a point-v1 reproduction directory from explicit options."""
    reproduce_dir = path / "reproduce"
    reproduce_dir.mkdir(parents=True, exist_ok=True)
    if settings.model_commit is None:
        settings.model_commit = huggingface_hub.model_info(settings.model).sha
    checkpoint = Path(checkpoint_path)
    _write_reproduction_files(reproduce_dir, settings, checkpoint.name, trial, options)
    if checkpoint.exists():
        (reproduce_dir / checkpoint.name).write_bytes(checkpoint.read_bytes())


def _uploaded_weight_hashes(info: Any) -> dict[str, str]:
    if not info.siblings:
        raise RuntimeError("Could not fetch uploaded model hashes.")
    hashes = {}
    for file in info.siblings:
        if not file.rfilename.endswith(".safetensors"):
            continue
        sha256 = getattr(file, "lfs", {}).get("sha256")
        if not sha256:
            raise RuntimeError("Could not fetch uploaded model hashes.")
        hashes[file.rfilename] = sha256
    return hashes


def upload_reproduce_folder(
    repo_id: str,
    settings: Settings,
    token: str,
    **options: Any,
) -> None:
    """Build and upload the point-v1 reproduction directory."""
    info = huggingface_hub.HfApi().model_info(
        repo_id=repo_id, files_metadata=True, token=token
    )
    options["uploaded_model_hashes"] = _uploaded_weight_hashes(info)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        create_reproduce_folder(root, settings, **options)
        for file_path in (root / "reproduce").iterdir():
            if file_path.is_file():
                huggingface_hub.upload_file(
                    path_or_fileobj=str(file_path),
                    path_in_repo=f"reproduce/{file_path.name}",
                    repo_id=repo_id,
                    token=token,
                )
