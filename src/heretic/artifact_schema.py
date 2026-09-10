# SPDX-License-Identifier: AGPL-3.0-or-later
"""Strict finite artifact schemas and acyclic identity bindings."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence, cast
from urllib.parse import urljoin
from urllib.request import urlopen

ACCEPTANCE_SCHEMA = "cara-acceptance-v2"
REPRODUCE_SCHEMA = "cara-reproduce-v2"
LEGACY_REPRODUCE_VERSIONS = frozenset({"3", "4"})


def build_trajectory_manifest(
    inputs: Any,
    good_continuations: Sequence[Any],
    bad_continuations: Sequence[Any],
) -> dict[str, Any]:
    """Bind prompts, continuations, rendering, and capture protocol fields."""
    from .protocol_data import fingerprint_dataset

    settings, model = inputs.settings, inputs.model
    return {
        "protocol": "trajectory-v2",
        "calibration": asdict(inputs.calibration_manifest),
        "good_dataset_fingerprint": fingerprint_dataset(
            settings.good_prompts, inputs.good_prompts
        ),
        "bad_dataset_fingerprint": fingerprint_dataset(
            settings.bad_prompts, inputs.bad_prompts
        ),
        "good_continuations": [asdict(item) for item in good_continuations],
        "bad_continuations": [asdict(item) for item in bad_continuations],
        "trajectory_tokens": settings.ara_trajectory_tokens,
        "trajectory_decay": settings.ara_trajectory_decay,
        "chat_template_sha256": model.model_manifest["chat_template_sha256"],
        "response_prefix_sha256": canonical_sha256(
            {"response_prefix": settings.response_prefix}
        ),
    }


def ensure_finite_json(value: Any, path: str = "$") -> None:
    """Reject non-finite numbers and values outside the JSON data model."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite value at {path}")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"non-string object key at {path}")
            ensure_finite_json(child, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            ensure_finite_json(child, f"{path}[{index}]")
        return
    raise ValueError(f"unsupported JSON value at {path}: {type(value).__name__}")


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Serialize finite JSON using one canonical UTF-8 representation."""
    ensure_finite_json(value)
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def canonical_sha256(value: Mapping[str, Any]) -> str:
    """Hash canonical finite JSON."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def manifest_differences(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> list[str]:
    """Return stable top-level field differences for an identity error."""
    keys = sorted(set(expected) | set(actual))
    return [key for key in keys if expected.get(key) != actual.get(key)]


def file_sha256(path: str | Path) -> str:
    """Hash a file with bounded reads."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def acceptance_binding(path: str | Path) -> dict[str, str]:
    """Return the stable external binding for an acceptance report path."""
    report_path = Path(path)
    if not report_path.is_file():
        return {"status": "missing"}
    report_bytes = report_path.read_bytes()
    report = json.loads(report_bytes)
    return {
        "status": str(report.get("status", "invalid")),
        "sha256": hashlib.sha256(report_bytes).hexdigest(),
        "path": report_path.name,
    }


def generate_sha256sums(hashes: Mapping[str, str]) -> str:
    """Render deterministic GNU-compatible binary checksum lines."""
    return "".join(
        f"{sha256} *{filename}\n" for filename, sha256 in sorted(hashes.items())
    )


def atomic_write_json(path: str | Path, value: Mapping[str, Any]) -> bytes:
    """Atomically write pretty finite JSON, flush it, and return exact bytes."""
    ensure_finite_json(value)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    data = (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    descriptor, temporary = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return data


def _required_strings(payload: Mapping[str, Any], fields: Sequence[str]) -> None:
    for field in fields:
        if not isinstance(payload.get(field), str) or not payload[field]:
            raise ValueError(f"artifact has no {field}")


def _validate_passed_acceptance_evidence(payload: Mapping[str, Any]) -> None:
    selected = payload["selected_trial_number"]
    if not isinstance(selected, int) or isinstance(selected, bool):
        raise ValueError("passed acceptance has no selected trial")
    expected_types = {
        "validation_replays": Mapping,
        "audit": list,
        "runtime_guard_report": Mapping,
        "study_summary": Mapping,
    }
    for field, expected in expected_types.items():
        if not isinstance(payload[field], expected):
            raise ValueError(f"passed acceptance {field} is invalid")
    replays = payload["validation_replays"]
    required_replays = {
        "first",
        "second",
        "max_absolute_error",
        "max_relative_error",
        "kl_drift",
    }
    if not required_replays.issubset(replays):
        raise ValueError("passed acceptance replay evidence is incomplete")
    if (
        not isinstance(replays["first"], list)
        or not isinstance(replays["second"], list)
        or not replays["first"]
        or not replays["second"]
        or not payload["audit"]
    ):
        raise ValueError("passed acceptance score evidence must not be empty")
    for field in ("max_absolute_error", "max_relative_error", "kl_drift"):
        value = replays[field]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"passed acceptance replay {field} is invalid")


def _validate_core_hash_manifest(hashes: Any) -> None:
    """Require every mandatory publication artifact to be hash-bound."""
    if not isinstance(hashes, Mapping) or not hashes:
        raise ValueError("core_artifact_hashes must be a non-empty object")
    required = {
        "README.md",
        "adapter_config.json",
        "effective-config.toml",
        "study-identity.json",
        "study.jsonl",
        "trajectory-manifest.json",
    }
    if not required.issubset(hashes) or not any(
        name.endswith(".safetensors") for name in hashes
    ):
        raise ValueError("core artifact hashes omit mandatory adapter files")
    for name, digest in hashes.items():
        path = PurePosixPath(name)
        if (
            not isinstance(name, str)
            or not name
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in name
        ):
            raise ValueError("core artifact hash path is invalid")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest.lower())
        ):
            raise ValueError(f"core artifact hash is invalid: {name}")


def validate_acceptance_v2(payload: Mapping[str, Any]) -> None:
    """Validate required fields and pass/fail consistency for acceptance v2."""
    ensure_finite_json(payload)
    if payload.get("schema") != ACCEPTANCE_SCHEMA:
        raise ValueError("unsupported acceptance schema")
    if payload.get("status") not in {"passed", "failed"}:
        raise ValueError("acceptance status must be passed or failed")
    _required_strings(payload, ("study_fingerprint",))
    if payload["status"] == "failed":
        _required_strings(payload, ("failure_stage",))
        if not isinstance(payload.get("audit_consumed"), bool):
            raise ValueError("failed acceptance must record audit consumption")
        selected = payload.get("selected_trial_number")
        if selected is not None and (
            not isinstance(selected, int) or isinstance(selected, bool)
        ):
            raise ValueError("failed acceptance selected trial is invalid")
        return
    _required_strings(
        payload,
        (
            "model_fingerprint",
            "data_fingerprint",
            "trajectory_fingerprint",
            "audit_ledger_sha256",
        ),
    )
    required = (
        "selected_trial_number",
        "validation_replays",
        "audit",
        "runtime_guard_report",
        "study_summary",
        "core_artifact_hashes",
    )
    if any(field not in payload for field in required):
        raise ValueError("passed acceptance is missing required evidence")
    _validate_passed_acceptance_evidence(payload)
    if payload.get("audit_consumed") is not True:
        raise ValueError("passed acceptance must consume exactly one audit")
    _validate_core_hash_manifest(payload["core_artifact_hashes"])
    forbidden = {"acceptance.json", "reproduce.json", "audit-ledger.json"}
    if forbidden & set(payload["core_artifact_hashes"]):
        raise ValueError("core hashes contain a self-referential artifact")


def validate_reproduce_v2(payload: Mapping[str, Any]) -> None:
    """Validate the trajectory-v2 reproduction identity envelope."""
    ensure_finite_json(payload)
    if payload.get("schema") != REPRODUCE_SCHEMA:
        raise ValueError("unsupported trajectory reproduce schema")
    _required_strings(
        payload,
        (
            "objective_version",
            "trajectory_manifest_sha256",
            "prefix_set_sha256",
            "search_space_version",
            "sampler_protocol",
            "acceptance_sha256",
            "model_fingerprint",
            "study_fingerprint",
            "data_fingerprint",
        ),
    )
    if payload["objective_version"] != "trajectory-v2":
        raise ValueError("trajectory reproduce objective version is invalid")
    hashes = cast(Mapping[str, str], payload.get("core_artifact_hashes"))
    _validate_core_hash_manifest(hashes)
    if set(hashes) & {"acceptance.json", "reproduce.json", "audit-ledger.json"}:
        raise ValueError("reproduce core hashes contain a forbidden artifact")
    selected = payload.get("selected_trial_number")
    if not isinstance(selected, int) or isinstance(selected, bool):
        raise ValueError("trajectory reproduce has no selected trial")
    if not isinstance(payload.get("parameters"), Mapping):
        raise ValueError("trajectory reproduce has no parameter envelope")
    from .ara_search import parse_parameter_envelope

    parse_parameter_envelope(payload["parameters"])


def parse_reproduce(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Dispatch legacy reproduce v3/v4 or strict trajectory-v2 envelopes."""
    if payload.get("schema") == "cara-research-reproduce-v3":
        from .ara_research_schema import validate_research_reproduce
        validate_research_reproduce(dict(payload))
        return payload
    if payload.get("schema") == REPRODUCE_SCHEMA:
        validate_reproduce_v2(payload)
        return payload
    if str(payload.get("version")) in LEGACY_REPRODUCE_VERSIONS:
        return payload
    raise ValueError("unsupported reproduce schema")


def validate_acceptance_reproduce_binding(
    acceptance: Mapping[str, Any],
    reproduce: Mapping[str, Any],
) -> None:
    """Validate the immutable cross-file identity fields without disk access."""
    if reproduce.get("schema") == "cara-research-reproduce-v3":
        from .ara_research_acceptance import validate_research_binding
        validate_research_binding(dict(acceptance), dict(reproduce))
        return
    validate_acceptance_v2(acceptance)
    validate_reproduce_v2(reproduce)
    if acceptance["status"] != "passed":
        raise ValueError("bound acceptance report is not passed")
    if acceptance["core_artifact_hashes"] != reproduce["core_artifact_hashes"]:
        raise ValueError("acceptance and reproduce core hashes differ")
    for field in (
        "model_fingerprint",
        "study_fingerprint",
        "data_fingerprint",
        "selected_trial_number",
    ):
        if acceptance[field] != reproduce[field]:
            raise ValueError(f"publication identity mismatch: {field}")
    if acceptance["trajectory_fingerprint"] != reproduce["trajectory_manifest_sha256"]:
        raise ValueError("acceptance and reproduce trajectory identities differ")


def _bound_report_bytes(source: str, name: str, same_directory: bool) -> bytes:
    if source.lower().startswith(("http://", "https://")):
        normalized = source.replace("/blob/", "/raw/").replace(
            "/src/branch/", "/raw/branch/"
        )
        relative = name if same_directory else f"../{name}"
        return urlopen(urljoin(normalized, relative)).read()
    parent = Path(source).resolve().parent
    report = parent / name if same_directory else parent.parent / name
    return report.read_bytes()


def load_bound_acceptance(
    source: str,
    reproduction: Mapping[str, Any],
    required: bool,
) -> dict[str, Any] | None:
    """Load and validate the acceptance report bound to a reproduction file."""
    is_v2 = reproduction.get("schema") in {
        REPRODUCE_SCHEMA, "cara-research-reproduce-v3"
    }
    binding = reproduction.get("acceptance")
    if is_v2:
        name = "acceptance.json"
        expected_hash = reproduction.get("acceptance_sha256")
    elif isinstance(binding, Mapping) and binding.get("status") == "passed":
        name = str(binding.get("path", ""))
        expected_hash = binding.get("sha256")
    elif required:
        raise ValueError("gated reproduction has no passed acceptance binding")
    else:
        return None
    if not name or Path(name).name != name:
        raise ValueError("acceptance binding path must be a file name")
    report_bytes = _bound_report_bytes(source, name, is_v2)
    if hashlib.sha256(report_bytes).hexdigest() != expected_hash:
        raise ValueError("acceptance report hash does not match reproduce.json")
    report = json.loads(report_bytes)
    if not isinstance(report, dict):
        raise ValueError("acceptance binding is malformed")
    if is_v2:
        validate_acceptance_reproduce_binding(report, reproduction)
    else:
        from .trial_methods import validate_acceptance_binding

        hashes = reproduction.get("hashes")
        if not isinstance(hashes, Mapping):
            raise ValueError("acceptance binding is malformed")
        validate_acceptance_binding(report, reproduction, hashes)
    return report


def reproduction_metadata(settings: Any, trial: Any) -> dict[str, Any]:
    """Build legacy-compatible environment fields for a strict v2 envelope."""
    from datetime import datetime, timezone

    from .utils import _reproduce_base_data

    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    payload = _reproduce_base_data(settings, trial, timestamp, {})
    payload.pop("system", None)
    return payload


def core_artifact_hashes(
    root: str | Path,
    relative_paths: Sequence[str],
) -> dict[str, str]:
    """Hash an explicit, non-self-referential core artifact set."""
    forbidden = {"acceptance.json", "reproduce.json", "audit-ledger.json"}
    if forbidden & set(relative_paths):
        raise ValueError("core artifact list contains a forbidden file")
    base = Path(root)
    resolved_base = base.resolve()
    hashes = {}
    for relative in sorted(relative_paths):
        candidate = Path(relative)
        if (
            not relative
            or candidate.is_absolute()
            or ".." in candidate.parts
            or candidate.as_posix() != relative
        ):
            raise ValueError(f"core artifact path is invalid: {relative}")
        path = base / candidate
        if not path.resolve().is_relative_to(resolved_base):
            raise ValueError(f"core artifact escapes publication root: {relative}")
        if not path.is_file():
            raise ValueError(f"core artifact is missing: {relative}")
        hashes[relative] = file_sha256(path)
    return hashes


def _verify_audit_ledger(base: Path, acceptance: Mapping[str, Any]) -> None:
    ledger_path = base / "audit-ledger.json"
    if not ledger_path.is_file():
        raise ValueError("published audit ledger is missing")
    if file_sha256(ledger_path) != acceptance["audit_ledger_sha256"]:
        raise ValueError("acceptance audit ledger hash does not match disk")
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    if (
        ledger.get("schema") != "cara-audit-ledger-v1"
        or ledger.get("status") != "passed"
        or ledger.get("audit_consumed") is not True
    ):
        raise ValueError("published audit ledger is not passed and consumed")


def _verify_publication_identities(
    base: Path,
    acceptance: Mapping[str, Any],
    reproduce: Mapping[str, Any],
) -> None:
    for field in (
        "model_fingerprint",
        "study_fingerprint",
        "data_fingerprint",
        "selected_trial_number",
    ):
        if reproduce[field] != acceptance[field]:
            raise ValueError(f"publication identity mismatch: {field}")
    trajectory = json.loads(
        (base / "trajectory-manifest.json").read_text(encoding="utf-8")
    )
    trajectory_hash = canonical_sha256(trajectory)
    if trajectory_hash != acceptance["trajectory_fingerprint"]:
        raise ValueError("acceptance trajectory fingerprint does not match disk")
    if trajectory_hash != reproduce["trajectory_manifest_sha256"]:
        raise ValueError("reproduce trajectory fingerprint does not match disk")
    study = json.loads((base / "study-identity.json").read_text(encoding="utf-8"))
    expected = (
        reproduce["study_fingerprint"],
        reproduce["selected_trial_number"],
        reproduce["parameters"],
    )
    if (
        study.get("study_fingerprint"),
        study.get("trial_number"),
        study.get("parameters"),
    ) != expected:
        raise ValueError("published study/candidate identity does not match")


def verify_artifact_graph(root: str | Path) -> None:
    """Re-read and verify core -> acceptance -> reproduce hash bindings."""
    base = Path(root)
    acceptance_path = base / "acceptance.json"
    reproduce_path = base / "reproduce.json"
    acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
    reproduce = json.loads(reproduce_path.read_text(encoding="utf-8"))
    if reproduce.get("schema") == "cara-research-reproduce-v3":
        from .ara_research_acceptance import verify_research_artifact_graph
        verify_research_artifact_graph(base)
        return
    validate_acceptance_v2(acceptance)
    validate_reproduce_v2(reproduce)
    validate_acceptance_reproduce_binding(acceptance, reproduce)
    expected = acceptance["core_artifact_hashes"]
    if core_artifact_hashes(base, list(expected)) != expected:
        raise ValueError("acceptance core artifact hashes do not match disk")
    if reproduce["core_artifact_hashes"] != expected:
        raise ValueError("reproduce core hashes differ from acceptance")
    if reproduce["acceptance_sha256"] != file_sha256(acceptance_path):
        raise ValueError("reproduce acceptance hash does not match disk")
    _verify_audit_ledger(base, acceptance)
    _verify_publication_identities(base, acceptance, reproduce)
