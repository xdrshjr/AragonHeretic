# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data isolation and immutable prompt bundles for trajectory-v2."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .ara_config import DatasetSpecification
from .utils import Prompt


class ProtocolDataError(ValueError):
    """Raised when a protocol dataset is ambiguous, invalid, or overlapping."""


@dataclass(frozen=True, order=True)
class AbsoluteSplit:
    """A deterministic zero-based half-open interval in a named split."""

    name: str
    start: int
    stop: int

    @property
    def size(self) -> int:
        """Return the number of selected rows."""
        return self.stop - self.start


@dataclass(frozen=True)
class RoleDataset:
    """A dataset specification assigned to one protocol role."""

    role: str
    specification: DatasetSpecification


@dataclass(frozen=True)
class ProtocolPromptBundle:
    """Materialized prompts bound to their complete dataset identity."""

    role: str
    prompts: tuple[Prompt, ...]
    row_indices: tuple[int, ...]
    dataset_fingerprint: str


@dataclass(frozen=True)
class DatasetMetadata:
    """Metadata required to validate audit data without reading any rows."""

    split_lengths: Mapping[str, int]
    columns: frozenset[str]


_ABSOLUTE_SPLIT = re.compile(r"^([A-Za-z0-9_.-]+)\[(\d*):(\d+)\]$")


def parse_absolute_split(value: str | None) -> AbsoluteSplit:
    """Parse a finite absolute split slice such as ``train[400:500]``.

    Args:
        value: Dataset split expression.

    Returns:
        Parsed half-open split interval.

    Raises:
        ProtocolDataError: If the expression is not a finite absolute slice.
    """
    match = _ABSOLUTE_SPLIT.fullmatch(value or "")
    if match is None or "%" in (value or ""):
        raise ProtocolDataError("protocol split must be an absolute finite slice")
    start = int(match.group(2) or 0)
    stop = int(match.group(3))
    if stop <= start:
        raise ProtocolDataError("protocol split stop must be greater than start")
    return AbsoluteSplit(match.group(1), start, stop)


def dataset_identity(specification: DatasetSpecification) -> dict[str, Any]:
    """Return the canonical identity fields for a dataset role."""
    interval = parse_absolute_split(specification.split)
    if not specification.commit:
        raise ProtocolDataError("protocol datasets must pin a revision")
    if not specification.column:
        raise ProtocolDataError("protocol datasets must name a prompt column")
    return {
        "dataset": specification.dataset,
        "revision": specification.commit,
        "split": interval.name,
        "start": interval.start,
        "stop": interval.stop,
        "column": specification.column,
        "prefix": specification.prefix,
        "suffix": specification.suffix,
        "system_prompt": specification.system_prompt,
    }


def fingerprint_dataset(
    specification: DatasetSpecification,
    prompts: Sequence[Prompt] | None = None,
) -> str:
    """Hash a dataset identity and, when present, normalized prompt contents."""
    try:
        payload = dataset_identity(specification)
    except ProtocolDataError:
        payload = specification.model_dump(
            exclude={"residual_plot_label", "residual_plot_color"}
        )
    if prompts is not None:
        payload["prompts"] = [
            {"system": prompt.system, "user": prompt.user} for prompt in prompts
        ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def prompt_spec_cache_key(specification: DatasetSpecification) -> str:
    """Return the stable in-process cache key for one prompt specification."""
    return json.dumps(
        specification.model_dump(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def validate_role_overlaps(datasets: Sequence[RoleDataset]) -> None:
    """Reject row overlap across protocol roles.

    Duplicate specifications within the same role are intentionally allowed. This
    permits Keywords and continuous scoring to share harmful validation data.
    """
    parsed = [(item, dataset_identity(item.specification)) for item in datasets]
    for left_index, (left, left_id) in enumerate(parsed):
        for right, right_id in parsed[left_index + 1 :]:
            if left.role == right.role:
                if left_id != right_id:
                    raise ProtocolDataError(
                        f"{left.role} datasets must have identical identities"
                    )
                continue
            comparable = (
                left_id["dataset"],
                left_id["revision"],
                left_id["split"],
            ) == (
                right_id["dataset"],
                right_id["revision"],
                right_id["split"],
            )
            overlaps = max(left_id["start"], right_id["start"]) < min(
                left_id["stop"], right_id["stop"]
            )
            if comparable and overlaps:
                raise ProtocolDataError(
                    f"protocol roles {left.role!r} and {right.role!r} overlap"
                )


def validate_role_sizes(
    datasets: Sequence[RoleDataset], expected_sizes: Mapping[str, int]
) -> None:
    """Require every protocol role to use its pre-registered row count."""
    for item in datasets:
        expected = expected_sizes.get(item.role)
        if expected is None:
            raise ProtocolDataError(f"protocol role {item.role!r} has no size contract")
        actual = parse_absolute_split(item.specification.split).size
        if actual != expected:
            raise ProtocolDataError(
                f"protocol role {item.role!r} has {actual} rows; expected {expected}"
            )


def materialize_prompt_bundle(
    role: str,
    specification: DatasetSpecification,
    loader: Callable[[DatasetSpecification], list[Prompt]],
) -> ProtocolPromptBundle:
    """Load, validate, and bind prompts for a non-audit protocol role."""
    interval = parse_absolute_split(specification.split)
    prompts = loader(specification)
    if len(prompts) != interval.size:
        raise ProtocolDataError(
            f"{role} expected {interval.size} rows, loaded {len(prompts)}"
        )
    if any(
        not isinstance(prompt.user, str) or not prompt.user.strip()
        for prompt in prompts
    ):
        raise ProtocolDataError(f"{role} contains an empty prompt")
    return ProtocolPromptBundle(
        role=role,
        prompts=tuple(prompts),
        row_indices=tuple(range(interval.start, interval.stop)),
        dataset_fingerprint=fingerprint_dataset(specification, prompts),
    )


def _load_hugging_face_metadata(
    specification: DatasetSpecification,
) -> DatasetMetadata:
    from datasets import load_dataset_builder

    builder = load_dataset_builder(
        specification.dataset,
        revision=specification.commit,
    )
    info = builder.info
    lengths = {
        name: int(split.num_examples) for name, split in (info.splits or {}).items()
    }
    columns = frozenset((info.features or {}).keys())
    return DatasetMetadata(lengths, columns)


def preflight_audit_metadata(
    specification: DatasetSpecification,
    loader: Callable[[DatasetSpecification], DatasetMetadata] | None = None,
) -> str:
    """Validate audit boundaries and schema without materializing audit rows."""
    identity = dataset_identity(specification)
    metadata = (loader or _load_hugging_face_metadata)(specification)
    split_length = metadata.split_lengths.get(identity["split"])
    if split_length is None:
        raise ProtocolDataError(f"audit split {identity['split']!r} is missing")
    if identity["stop"] > split_length:
        raise ProtocolDataError("audit split exceeds the dataset row count")
    if identity["column"] not in metadata.columns:
        raise ProtocolDataError("audit prompt column is missing")
    return fingerprint_dataset(specification)
