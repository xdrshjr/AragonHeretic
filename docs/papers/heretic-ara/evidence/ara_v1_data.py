"""Replay the archived point-v1 experiment without importing research code."""

import ast
import collections
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import subprocess
import sys
import tomllib

sys.dont_write_bytecode = True
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_SOURCE = REPO_ROOT / "docs/logs/ara-v1"
JOURNAL = "checkpoints/--root--autodl-fs--models--Qwen3--8-27B.jsonl"
FIX_REVISION = "868ca73b63e6ceee196a6281c780d6c2dec14a05"
V2_REVISION = "2871bc19050617377f011ecbf1d64c440f2d6a96"
# Reviewed checkout baseline, not the live HEAD of a later paper-only commit.
ANALYSIS_BASE_REVISION = "1a00901895f984e0c52737b89530a0681e91fd8a"
SOURCE_FILES = (
    "ara.py ara_trajectory.py ara_runtime.py ara_search.py ara_config.py "
    "trial_methods.py study_runner.py protocol_data.py acceptance.py "
    "acceptance_export.py artifact_schema.py model.py targeting.py "
    "workflow.py config.py evaluator.py plugin.py scorer.py "
    "continuation_scores.py scorers/keyword_rate.py "
    "scorers/kl_divergence.py scorers/refusal_log_odds.py"
).split()
HISTORICAL_FILES = (
    "ara.py", "trial_methods.py", "scorers/keyword_rate.py",
    "scorers/kl_divergence.py",
)
ARCHIVE_HASHES = dict(zip(
    ["acceptance.json", JOURNAL, "config.toml", "exit-code.txt",
     "gpu-after.csv", "gpu-before.csv", "python-version.txt", "run.log",
     "source-commit.txt"],
    ["41c474dff52c8f07b9297f8005caaa989f91e75fc13a0b798cc7eeec5cbfa7fe",
     "740c04d6b9bd0f810b41fac38a38f1eadf2638f213fdaff4a7eb036e11c5bf1f",
     "a1c6f6bb7d2382952cf7761018bf6b5db70dbbcc6b621c70f6776088a8307bed",
     "9a271f2a916b0b6ee6cecb2426f0b3206ef074578be55d9bc94f6f3fe3ab86aa",
     "1dd7aaf6c41a812ce5b1668ca280a4635e53ad71a1adef3e4db3e40ad25bab0c",
     "9a453a13e02a9c2e390495abb1eefd6038ebab40344dcbba31260c116fd7b3d4",
     "55ae85cf4bdb38743edbcd53ea68ff36511997ec6c21b1e83d8bebc939bf056b",
     "3c67b21c18da37c51c4d68031e18808714eac382238c17a3bd8955e080b4e577",
     "26507304e096f5a211d1a2f2b5dde80bbc392e0673ea51728f7ad63b022e275d"],
    strict=True,
))
OP_FIELDS = {
    0: "study_name directions", 2: "study_id user_attr",
    4: "study_id datetime_start",
    5: "trial_id param_name param_value_internal distribution",
    6: "trial_id state values datetime_complete",
    8: "trial_id user_attr", 9: "trial_id system_attr",
}
STATES = {1: "COMPLETE", 2: "PRUNED", 3: "FAIL"}
FINGERPRINTS = (
    "model_fingerprint", "study_fingerprint", "calibration_fingerprint",
)
UNSAVED_FIELDS = {
    "max_batch_size", "study_checkpoint_dir", "save_directory",
    "acceptance_gate.report_path",
}
ACCEPTANCE_FIELDS = (
    "selected_trial_number", "validation_scores", "audit_scores",
    "reload_scores", "validation_replay_scores", "artifact_hashes",
)


class InputError(ValueError):
    """An archive, source, or protocol cannot support this manuscript."""


def require(condition, message):
    """Raise InputError with message unless condition is true."""
    if not condition:
        raise InputError(message)


def sha256(payload):
    """Return the SHA-256 hex digest of bytes; accepts no decoded text."""
    return hashlib.sha256(payload).hexdigest()


def _reject_constant(value):
    raise InputError(f"non-finite JSON number: {value}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json(payload, location="JSON"):
    """Decode strict JSON bytes/text.

    Args:
        payload: UTF-8 bytes or decoded JSON text.
        location: Source description included in errors.
    Returns:
        A JSON-compatible Python value.
    Raises:
        InputError: Encoding, syntax, duplicate keys or non-finite numbers.
    """
    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        result = json.loads(
            payload, parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
        # A legal numeric literal such as 1e999 also overflows Python float.
        json.dumps(result, allow_nan=False)
        return result
    except (ValueError, UnicodeError) as error:
        raise InputError(f"{location}: {error}") from error


def physical_lines(payload):
    """Decode physical lines without splitting CR refreshes.

    Args:
        payload: Raw UTF-8 bytes.
    Yields:
        Tuples of 1-based LF line number and decoded text.
    Raises:
        InputError: A physical line is not valid UTF-8.
    """
    for number, raw in enumerate(payload.split(b"\n"), 1):
        try:
            yield number, raw.decode("utf-8")
        except UnicodeError as error:
            raise InputError(f"UTF-8 error at LF line {number}") from error


def log_fragments(payload):
    """Locate terminal refresh fragments within physical lines.

    Args:
        payload: Raw UTF-8 log bytes.
    Yields:
        Dictionaries with line, fragment and text; both indices start at 1.
    Raises:
        InputError: The log is not valid UTF-8.
    """
    for number, line in physical_lines(payload):
        for fragment, text in enumerate(line.split("\r"), 1):
            if text:
                yield {"line": number, "fragment": fragment, "text": text}


def read_git_blob(revision, path):
    """Read a fixed Git blob without shell interpolation.

    Args:
        revision: Full 40-character commit SHA.
        path: Repository-relative POSIX source path.
    Returns:
        Original blob bytes, without newline conversion.
    Raises:
        InputError: Revision is invalid or the historical object is missing.
    """
    require(bool(re.fullmatch(r"[0-9a-f]{40}", revision)),
            f"full revision required: {revision}")
    try:
        result = subprocess.run(
            ["git", "show", f"{revision}:{path}"], cwd=REPO_ROOT,
            capture_output=True, timeout=30, check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise InputError(f"missing historical source {revision}:{path}") \
            from error
    return result.stdout


def _source_record(path, payload, kind, expected=None, revision=None):
    relative = path.relative_to(REPO_ROOT).as_posix()
    prefix = f"git:{revision}" if revision else "repo"
    return {
        "source_id": f"{prefix}:{relative}", "source_kind": kind,
        "path": relative, "revision": revision, "sha256": sha256(payload),
        "expected_sha256": expected, "archive_member": expected is not None,
        "line_start": 1,
        "line_end": len(payload.split(b"\n")) - int(payload.endswith(b"\n")),
    }


def _archive_sources(source):
    declared = {}
    for number, line in physical_lines((source / "SHA256SUMS").read_bytes()):
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](.+?)\r?", line)
        require(match is not None, f"SHA256SUMS LF line {number}: format")
        digest, name = match.groups()
        target = (source / name).resolve()
        require(target.is_relative_to(source), f"archive path escape: {name}")
        require(name not in declared, f"duplicate archive member: {name}")
        declared[name] = digest
    require(declared == ARCHIVE_HASHES, "original nine SHA256SUMS mismatch")
    records = []
    for name, expected in declared.items():
        target = (source / name).resolve()
        payload = target.read_bytes()
        require(sha256(payload) == expected, f"SHA-256 mismatch: {target}")
        records.append(_source_record(target, payload, "archive", expected))
    for name in ("SHA256SUMS", "SUMMARY.md"):
        target = source / name
        records.append(_source_record(target, target.read_bytes(), "archive"))
    return records


def collect_sources(source):
    """Validate archive hashes and return SourceRecords; raise InputError.

    Args:
        source: Archive directory within the workspace.
    Returns:
        Original archive records, supplemental files, and four Git blobs.
    Raises:
        InputError: An input is missing, outside the workspace or invalid.
    """
    source = Path(source).resolve()
    require(source.is_relative_to(REPO_ROOT), "source outside workspace")
    try:
        records = _archive_sources(source)
        paths = [f"src/heretic/{name}" for name in SOURCE_FILES]
        paths.append("config.qwen38-27b-cara-v2.toml")
        for name in paths:
            target = REPO_ROOT / name
            records.append(_source_record(
                target, target.read_bytes(), "worktree",
            ))
        for name in HISTORICAL_FILES:
            path = f"src/heretic/{name}"
            records.append(_source_record(
                REPO_ROOT / path, read_git_blob(FIX_REVISION, path),
                "git_blob", revision=FIX_REVISION,
            ))
        return records
    except OSError as error:
        raise InputError(f"source unavailable: {error}") from error


def _validate_operation(record, number):
    require(isinstance(record, dict), f"line {number}: expected object")
    opcode = record.get("op_code")
    require(type(opcode) is int and opcode in OP_FIELDS,
            f"line {number}: unsupported op_code {opcode}")
    fields = set(OP_FIELDS[opcode].split()) | {"op_code", "worker_id"}
    require(set(record) == fields, f"line {number}: op {opcode} fields")
    require(isinstance(record["worker_id"], str), "invalid worker_id")
    if "study_id" in record:
        require(type(record["study_id"]) is int and record["study_id"] == 0,
                f"line {number}: study_id must be 0")
    return opcode


def _merge_attributes(target, record, key):
    attributes = record[key]
    require(isinstance(attributes, dict), f"{key} must be an object")
    target.update(attributes)


def _record_parameter(trial, record):
    name = record["param_name"]
    value = record["param_value_internal"]
    require(isinstance(name, str), "parameter name must be a string")
    require(name not in trial["raw_params"], f"duplicate parameter: {name}")
    require(type(value) in (int, float), f"non-number parameter: {name}")
    distribution = parse_json(record["distribution"], name)
    require(set(distribution) == {"name", "attributes"}, "distribution keys")
    require(distribution["name"] == "FloatDistribution", "distribution type")
    attributes = distribution["attributes"]
    require(set(attributes) == {"step", "low", "high", "log"},
            "unsupported FloatDistribution attributes")
    require(attributes["step"] is None, "stepped distribution unsupported")
    require(type(attributes["log"]) is bool, "distribution log must be bool")
    require(attributes["low"] <= value <= attributes["high"],
            f"parameter outside distribution: {name}")
    trial["raw_params"][name] = value
    trial["distributions"][name] = distribution


def _update_trial(trials, record, number):
    identifier = record["trial_id"]
    require(type(identifier) is int and 0 <= identifier < len(trials),
            f"line {number}: trial {identifier} not created")
    trial = trials[identifier]
    opcode = record["op_code"]
    require(trial["terminal_line"] is None, "update after terminal state")
    if opcode == 5:
        _record_parameter(trial, record)
    elif opcode == 8:
        _merge_attributes(trial["user_attrs"], record, "user_attr")
        if "scores" in record["user_attr"]:
            trial["score_lines"].append(number)
    elif opcode == 9:
        _merge_attributes(trial["system_attrs"], record, "system_attr")
    else:
        require(trial["terminal_line"] is None, "duplicate terminal state")
        require(type(record["state"]) is int and record["state"] in STATES,
                f"line {number}: unsupported terminal state")
        require(isinstance(record["datetime_complete"], str),
                "terminal datetime missing")
        trial.update(state=STATES[record["state"]], values=record["values"],
                     terminal_line=number)


def replay_journal(payload):
    """Replay single-study bytes into (study attributes, raw trial records).

    Args:
        payload: Raw UTF-8 journal bytes, split only at LF.
    Returns:
        A tuple of study attributes and creation-ordered trial dictionaries.
    Raises:
        InputError: Unsupported shape, ordering, operation, or incomplete trial.
    """
    study, trials, created = {}, [], False
    for number, line in physical_lines(payload):
        if not line.strip():
            continue
        record = parse_json(line, f"journal LF line {number}")
        opcode = _validate_operation(record, number)
        if opcode == 0:
            require(not created, "only one study is supported")
            directions = record["directions"]
            require(directions == [1, 1] and
                    all(type(value) is int for value in directions),
                    "directions must be [1,1]")
            created = True
            continue
        require(created, f"line {number}: study not created")
        if opcode == 2:
            _merge_attributes(study, record, "user_attr")
        elif opcode == 4:
            trials.append({
                "trial_number": len(trials), "user_attrs": {},
                "raw_params": {}, "distributions": {}, "system_attrs": {},
                "score_lines": [], "terminal_line": None,
            })
        else:
            _update_trial(trials, record, number)
    require(created, "journal has no study")
    for trial in trials:
        number = trial["trial_number"]
        require(trial["terminal_line"] is not None,
                f"trial {number}: missing terminal record")
        require(trial["user_attrs"].get("index") == number + 1,
                f"trial {number}: display index conflicts with creation order")
    return study, trials


def _flatten(value, prefix=""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _flatten(child, f"{prefix}.{key}".lstrip("."))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _flatten(child, f"{prefix}[{index}]")
    else:
        yield prefix, value


def compare_settings(config, effective, manifest):
    """Compare explicit TOML leaves with saved settings and study identity.

    Args:
        config: Decoded original TOML snapshot.
        effective: Decoded nested settings JSON.
        manifest: Saved study manifest, with scorer_settings naming.
    Returns:
        A list of default, unsaved and automatic-resolution differences.
    Raises:
        InputError: An explicitly configured semantic field conflicts.
    """
    configured, saved = dict(_flatten(config)), dict(_flatten(effective))
    differences = []
    for key, value in configured.items():
        observed = saved.get(key)
        if key in UNSAVED_FIELDS:
            require(key not in saved, f"unexpectedly saved field: {key}")
            kind = "not_saved"
        elif key == "batch_size" and value == 0:
            require(observed == config["max_batch_size"], "auto batch conflict")
            kind = "automatic_resolution"
        else:
            require(key in saved and observed == value,
                    f"effective setting conflict: {key}: {value} != {observed}")
            continue
        differences.append({"field": key, "kind": kind,
                            "configured": value, "effective": observed})
    for key, value in saved.items():
        if key not in configured:
            differences.append({"field": key, "kind": "default_added",
                                "configured": None, "effective": value})
    _check_study_manifest(manifest, effective)
    return differences


def _check_study_manifest(manifest, effective):
    for key, value in manifest.items():
        actual_key = "scorer" if key == "scorer_settings" else key
        if actual_key in effective:
            require(value == effective[actual_key],
                    f"study_manifest conflict: {key}")


def _score_number(score):
    require(isinstance(score, dict), "score must be an object")
    require(set(score) == {"value", "rich_display", "md_display"},
            "unsupported point-v1 score fields")
    value = score.get("value")
    require(type(value) in (int, float) and math.isfinite(value),
            "score must be a finite number")
    return value


def _keyword_count(score, expected):
    displays = [score.get(key) for key in ("md_display", "rich_display")]
    require(all(isinstance(value, str) for value in displays),
            "Keywords missing display count")
    counts = [re.fullmatch(r"(\d+)/(\d+)", value) for value in displays]
    require(all(counts), "Keywords malformed display count")
    pairs = [tuple(map(int, match.groups())) for match in counts]
    require(pairs[0] == pairs[1], "Keywords display counts disagree")
    count, denominator = pairs[0]
    require(denominator == expected and 0 <= count <= denominator,
            "Keywords sample denominator mismatch")
    require(math.isclose(count / denominator, _score_number(score),
                         rel_tol=0, abs_tol=1e-12), "Keywords count mismatch")
    return count


def _extract_scores(raw, expected):
    if raw["state"] != "COMPLETE":
        return dict.fromkeys(("keywords", "keyword_count", "sample_count",
                              "kl", "baseline_keywords", "baseline_kl"))
    scores = raw["user_attrs"].get("scores")
    require(isinstance(scores, list) and len(scores) == 2,
            "two scores required")
    named = {score["name"]: score for score in scores}
    require(set(named) == {"Keywords", "KL divergence"}, "score names invalid")
    keyword, kl = named["Keywords"], named["KL divergence"]
    values = [_score_number(keyword["score"]), _score_number(kl["score"])]
    terminal = raw["values"]
    require(isinstance(terminal, list) and len(terminal) == 2,
            "COMPLETE requires two terminal objectives")
    for value, actual in zip(values, terminal, strict=True):
        require(type(actual) in (int, float), "terminal objective not numeric")
        require(math.isclose(value, actual, rel_tol=0, abs_tol=1e-12),
                "terminal objective and score user_attr disagree")
    count = _keyword_count(keyword["score"], expected)
    _keyword_count(keyword["baseline"], expected)
    return {
        "keywords": values[0], "kl": values[1], "keyword_count": count,
        "sample_count": expected,
        "baseline_keywords": _score_number(keyword["baseline"]),
        "baseline_kl": _score_number(kl["baseline"]),
    }


def calculate_drops(baseline, keywords):
    """Calculate both meanings of keyword reduction.

    Args:
        baseline: Baseline keyword rate K0.
        keywords: Candidate keyword rate K.
    Returns:
        Absolute K0-K and relative (K0-K)/K0; the latter is None at K0=0.
    """
    absolute = baseline - keywords
    return absolute, absolute / baseline if baseline else None


def make_trial(raw, gate, fingerprints):
    """Build a TrialRecord from the structurally replayed journal.

    Args:
        raw: One trial returned by replay_journal.
        gate: Archived threshold settings, including expected_samples.
        fingerprints: Expected fingerprint names and values.
    Returns:
        A trial dictionary preserving scores, parameters and source lines.
    Raises:
        InputError: Named scores, denominators, objectives or identity conflict.
    """
    attrs, number = raw["user_attrs"], raw["trial_number"]
    for key, expected in fingerprints.items():
        require(attrs.get(key) == expected, f"trial {number}: {key} conflict")
    result = {key: raw[key] for key in (
        "trial_number", "state", "raw_params", "system_attrs", "score_lines",
        "terminal_line", "distributions",
    )}
    result.update(_extract_scores(raw, gate["expected_samples"]))
    result.update(
        display_index=number + 1,
        resolved_params=attrs.get("ara_parameters", {}),
        fingerprints={key: attrs[key] for key in fingerprints},
        module_summary=attrs.get("ara_summary", {}), pareto=False,
        numerical_gate_pass=False, absolute_drop=None, relative_drop=None,
        phase=("startup" if number < 36 else
               "search-36-79" if number < 80 else "search-80-119"),
    )
    if result["keywords"] is not None:
        absolute, relative = calculate_drops(
            result["baseline_keywords"], result["keywords"],
        )
        result.update(absolute_drop=absolute, relative_drop=relative)
        result["numerical_gate_pass"] = (
            result["keywords"] <= gate["keyword_max"]
            and absolute >= gate["keyword_drop_min"]
            and result["kl"] <= gate["kl_max"]
        )
    return result


def pareto_numbers(trials):
    """Select strictly nondominated candidates, retaining equal points.

    Args:
        trials: Scored trials containing keywords, kl and trial_number.
    Returns:
        The nondominated trial numbers in input order.
    """
    points = [(trial["keywords"], trial["kl"]) for trial in trials]
    return [trial["trial_number"] for trial, point in zip(trials, points)
            if not any(other[0] <= point[0] and other[1] <= point[1]
                       and other != point for other in points)]


def linear_quantile(values, probability):
    """Compute the sorted linear quantile at h=(n-1)p.

    Args:
        values: Nonempty sequence of finite numbers.
        probability: Probability p in [0,1].
    Returns:
        Interpolated value without display rounding.
    Raises:
        InputError: Sequence is empty or p is outside [0,1].
    """
    require(bool(values) and 0 <= probability <= 1, "invalid quantile input")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower, upper = math.floor(position), math.ceil(position)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def summarize_trials(trials):
    """Compute statistics of one candidate search, marking Pareto records.

    Args:
        trials: Scored COMPLETE TrialRecords, with unique trial numbers.
    Returns:
        Counts, selected display numbers, quantiles and phase statistics.
    Raises:
        InputError: The input is empty or contains unfinished/failed trials.
    """
    require(bool(trials) and all(t["state"] == "COMPLETE" for t in trials),
            "summary requires all COMPLETE trials")
    pareto = pareto_numbers(trials)
    for trial in trials:
        trial["pareto"] = trial["trial_number"] in pareto
    ordered = sorted(trials, key=lambda t: (
        t["keywords"], t["kl"], t["trial_number"],
    ))
    min_kl = min(trials, key=lambda t: (
        t["kl"], t["keywords"], t["trial_number"],
    ))
    return {
        "trial_count": len(trials),
        "state_counts": dict(collections.Counter(t["state"] for t in trials)),
        "numerical_gate_count": sum(t["numerical_gate_pass"] for t in trials),
        "pareto_trial_numbers": pareto,
        "best_keyword_trial": ordered[0]["trial_number"],
        "min_kl_trial": min_kl["trial_number"],
        "last_trial": max(t["trial_number"] for t in trials),
        "quantiles": _trial_quantiles(trials),
        "phase_statistics": _phase_statistics(trials),
    }


def _trial_quantiles(trials):
    return {key: [linear_quantile([t[key] for t in trials], p)
                  for p in (0, .25, .5, .75, 1)]
            for key in ("keywords", "kl")}


def _phase_statistics(trials):
    phases = []
    for first, last in ((0, 35), (36, 79), (80, 119)):
        values = [t["keywords"] for t in trials
                  if first <= t["trial_number"] <= last]
        if values:
            phases.append({"first_trial": first, "last_trial": last,
                           "count": len(values), "minimum": min(values),
                           "median": linear_quantile(values, .5),
                           "mean": statistics.mean(values)})
    return phases


def _validate_parameters(trial):
    raw, resolved = trial["raw_params"], trial["resolved_params"]
    names = ("layer_start_fraction layer_span_fraction attn_strength "
             "mlp_strength_raw push_weight margin").split()
    require(set(raw) == {f"ara.{name}" for name in names},
            "six params required")
    require(0 <= resolved["start_layer_index"] < resolved["end_layer_index"],
            "invalid half-open layer interval")
    for component, strength in (("attn.o_proj", raw["ara.attn_strength"]),
                                ("mlp.down_proj",
                                 max(0, raw["ara.mlp_strength_raw"]))):
        actual = resolved["components"][component]
        expected = dict(strength=strength, push_weight=raw["ara.push_weight"],
                        margin=raw["ara.margin"])
        require(actual == expected, "resolved component parameters conflict")


def _load_log_evidence(source, settings):
    fragments = list(log_fragments((source / "run.log").read_bytes()))
    expected = settings["acceptance_gate"]["expected_samples"]
    samples = f"* {expected} prompts loaded"
    require(sum(f["text"].strip() == samples for f in fragments) == 2,
            "run.log must confirm both validation sample counts")
    batch = f"* Chosen batch size: {settings['batch_size']}"
    require(any(f["text"].strip() == batch for f in fragments),
            "run.log effective batch size mismatch")
    baseline = f"* Baseline Keywords: {expected}/{expected}"
    require(any(f["text"].strip() == baseline for f in fragments),
            "run.log baseline denominator mismatch")
    needles = ("prompts loaded", "Chosen batch", "Baseline", "Elapsed time:",
               "Resident system RAM:", "Allocated GPU VRAM:", "gate failed")
    return [f for f in fragments if any(word in f["text"] for word in needles)]


def _read_default_markers():
    path = REPO_ROOT / "src/heretic/scorers/keyword_rate.py"
    tree = ast.parse(path.read_bytes())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [getattr(target, "id", "") for target in node.targets]
            if "DEFAULT_KEYWORD_MARKERS" in names:
                markers = ast.literal_eval(node.value)
                require(isinstance(markers, list), "default markers not list")
                require(all(isinstance(item, str) for item in markers),
                        "default markers not strings")
                return markers
    raise InputError("default markers source assignment missing")


def _run_identity(source, settings, fingerprints):
    revision = (source / "source-commit.txt").read_text().strip()
    require(bool(re.fullmatch(r"[0-9a-f]{40}", revision)), "run HEAD invalid")
    return {
        "model_label": Path(settings["model"]).name,
        "model_revision": settings.get("model_commit"),
        "recorded_run_head": revision, "later_fix_revision": FIX_REVISION,
        "trajectory_v2_revision": V2_REVISION,
        "analysis_base_revision": ANALYSIS_BASE_REVISION,
        "exact_run_worktree_available": False, "run_date": "2026-09-04",
        "objective_version": "point-v1", "version_status": "inferred",
        "version_evidence": ["cara-capture-v1", "cara-lbfgs-v1",
                             "cara-search-v1"],
        "fingerprints": fingerprints,
        "process_exit_code": int((source / "exit-code.txt").read_text()),
        "gpu_archive": (source / "gpu-before.csv").read_text().strip(),
        "python_archive": (source / "python-version.txt").read_text().strip(),
    }


def _load_validated_run(source):
    study, raw_trials = replay_journal((source / JOURNAL).read_bytes())
    settings = parse_json(study["settings"], "study settings")
    config = tomllib.loads((source / "config.toml").read_text("utf-8"))
    acceptance = parse_json((source / "acceptance.json").read_bytes())
    manifest = study["study_manifest"]
    versions = (manifest["calibration_protocol"], manifest["optimizer_schema"],
                manifest["search_space_version"])
    require(versions == ("cara-capture-v1", "cara-lbfgs-v1", "cara-search-v1"),
            "point-v1 schema inference unavailable")
    differences = compare_settings(config, settings, manifest)
    fingerprints = {key: acceptance[key] for key in FINGERPRINTS}
    for key, expected in fingerprints.items():
        require(bool(re.fullmatch(r"[0-9a-f]{64}", expected)),
                f"invalid acceptance {key}")
        if key in study:
            require(study[key] == expected, f"study {key} conflict")
    trials = [make_trial(t, settings["acceptance_gate"], fingerprints)
              for t in raw_trials]
    require(len(trials) == 120, "snapshot requires 120 trials")
    require(all(t["state"] == "COMPLETE" for t in trials),
            "snapshot requires 120 COMPLETE trials")
    for trial in trials:
        _validate_parameters(trial)
        require(trial["baseline_keywords"] == 1 and trial["baseline_kl"] == 0,
                "baseline score conflict")
    return settings, config, acceptance, differences, trials


def _gate_counts(trials, gate):
    return {
        "keywords": sum(t["keywords"] <= gate["keyword_max"] for t in trials),
        "kl": sum(t["kl"] <= gate["kl_max"] for t in trials),
        "joint": sum(t["numerical_gate_pass"] for t in trials),
    }


def load_summary(source=DEFAULT_SOURCE):
    """Recompute a complete RunSummary from verified bytes; raise InputError.

    Args:
        source: Archive directory. Relative paths resolve from the caller cwd.
    Returns:
        Deterministic JSON-compatible summary; no files are written.
    Raises:
        InputError: Any archive, method source or protocol check fails.
    """
    source = Path(source).resolve()
    records = collect_sources(source)
    try:
        settings, config, acceptance, differences, trials = \
            _load_validated_run(source)
        fingerprints = {key: acceptance[key] for key in FINGERPRINTS}
        summary = {
            "schema_version": "heretic-ara-paper-v1",
            "source_records": records, "trials": trials,
            "run_identity": _run_identity(source, settings, fingerprints),
            "effective_settings": settings, "archived_config": config,
            "config_differences": differences,
            "score_definitions": {
                "keywords": "Refusal-keyword response rate; empty counts.",
                "kl": "Mean first-token D_KL(base || adapter), nats.",
                "sample_count_evidence": "display counts + TOML + run.log; "
                "old score records lack sample_count/dataset_fingerprint",
                "historical_drop": "absolute K0-K; v2 uses (K0-K)/K0",
                "v2_default_markers": _read_default_markers(),
                "v2_markers_source": "repo:src/heretic/scorers/keyword_rate.py",
            },
            "baseline": {"keywords": trials[0]["baseline_keywords"],
                         "kl": trials[0]["baseline_kl"],
                         "sample_count": trials[0]["sample_count"]},
            "gate_thresholds": settings["acceptance_gate"],
            "gate_counts": _gate_counts(trials, settings["acceptance_gate"]),
            "log_evidence": _load_log_evidence(source, settings),
            "v2_template": tomllib.loads((REPO_ROOT /
                "config.qwen38-27b-cara-v2.toml").read_text("utf-8")),
            "acceptance_status": acceptance["status"],
            "limitations": ["One adaptive validation search, not repetitions.",
                "No matched directional baseline or trajectory-v2 measurement.",
                "No sample-level semantic labels or independent audit scores.",
                "Run had uncommitted fixes; exact worktree is unavailable.",
                "Model revision is not pinned; fingerprint cannot recover it."],
        }
        summary.update({key: acceptance[key] for key in ACCEPTANCE_FIELDS})
        return summary | summarize_trials(trials)
    except (KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        raise InputError(f"archive/schema mismatch: {error}") from error
