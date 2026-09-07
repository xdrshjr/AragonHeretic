"""Validate the frozen ARA v1 archive and render reproducible paper evidence.

Python 3.11+ standard library only. This reader deliberately supports one pinned
archive dialect; it neither evaluates journal strings nor opens Optuna storage.
"""

import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys
import tempfile
import tomllib


PAPER = Path(__file__).resolve().parents[1]
WORKSPACE = PAPER.parents[2]
ARCHIVE = WORKSPACE / "docs/logs/ara-v1"
MANIFEST_HASH = (
    "1ff138f8be7c4361a828fadc93db071c8357d10e793d8520be669862d58f1a29"
)
JOURNAL = "checkpoints/--root--autodl-fs--models--Qwen3--8-27B.jsonl"
ARCHIVE_NAMES = {
    "acceptance.json", JOURNAL, "config.toml", "exit-code.txt",
    "gpu-after.csv", "gpu-before.csv", "python-version.txt", "run.log",
    "source-commit.txt",
}
FIX_COMMIT = "868ca73b63e6ceee196a6281c780d6c2dec14a05"
RECORDED_COMMIT = "cd2977a3c7feda14c475d9912f21c884aca1fd5d"
PARAMETERS = {
    "layer_start_fraction": (0.25, 0.65, False),
    "layer_span_fraction": (0.10, 0.55, False),
    "attn_strength": (0.001, 1.0, True),
    "mlp_strength_raw": (-0.10, 0.50, False),
    "push_weight": (0.0, 2.0, False),
    "margin": (0.25, 4.0, True),
}
SCORE_NAMES = ["Keywords", "KL divergence"]
IDENTITIES = (
    "model_fingerprint", "study_fingerprint", "calibration_fingerprint"
)
RESULT_FIELDS = (
    "selected_trial_number parameters validation_scores audit_scores "
    "reload_scores validation_replay_scores parameter_comparison score_drift "
    "module_counts resource_peaks environment_versions failure_trials "
    "artifact_hashes"
).split()
CSV_COLUMNS = (
    "trial_number,state,keywords,kl_divergence,baseline_keywords,"
    "relative_keyword_drop,passes_keyword_max,passes_keyword_drop,passes_kl,"
    "passes_all,pareto,running_best_keywords,layer_start_fraction,"
    "layer_span_fraction,attn_strength,mlp_strength_raw,mlp_strength_applied,"
    "push_weight,margin,start_layer_index,end_layer_index,processed_modules,"
    "skipped_modules,failed_modules"
).split(",")
OUTPUTS = (
    "evidence/ara-v1-summary.json", "evidence/ara-v1-trials.csv",
    "tables/ara-v1-protocol.tex", "tables/ara-v1-results.tex",
)


class EvidenceError(ValueError):
    """Malformed, inconsistent, unsafe, or stale evidence."""


def require(condition, location, message):
    """Raise a contextual evidence error when an invariant fails."""
    if not condition:
        raise EvidenceError(f"{location}: {message}")


def finite_number(value, location):
    """Require a finite JSON number, excluding booleans."""
    require(type(value) in (int, float), location, "expected number")
    require(math.isfinite(value), location, "nonfinite number")
    return value


def integer(value, location):
    """Require an integer, excluding booleans."""
    require(type(value) is int, location, "expected integer")
    return value


def unique_object(pairs):
    """Decode JSON objects without silently overwriting duplicate keys."""
    result = {}
    for key, value in pairs:
        require(key not in result, key, "duplicate JSON object key")
        result[key] = value
    return result


def validate_finite_tree(value, location):
    """Reject overflowed JSON numeric literals and nonfinite fixture values."""
    if isinstance(value, dict):
        for key, child in value.items():
            validate_finite_tree(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            validate_finite_tree(child, f"{location}[{index}]")
    elif type(value) is float:
        finite_number(value, location)


def parse_json(value, location):
    """Parse strict JSON with duplicate-key and nonfinite rejection."""
    require(isinstance(value, str), location, "expected JSON string")
    try:
        parsed = json.loads(value, object_pairs_hook=unique_object)
        validate_finite_tree(parsed, location)
        return parsed
    except (ValueError, RecursionError) as error:
        raise EvidenceError(f"{location}: {error}") from error


def safe_relative(root, name):
    """Resolve a portable relative name without traversal or symlink escape."""
    posix, windows = PurePosixPath(name), PureWindowsPath(name)
    require(name and "\\" not in name, name, "unsafe path spelling")
    require(not posix.is_absolute() and not windows.drive, name,
            "absolute or drive-qualified path")
    require(".." not in posix.parts, name, "path traversal")
    destination = (root / posix).resolve()
    require(destination.is_relative_to(root), name, "symlink escapes root")
    return destination


def parse_manifest(raw, root):
    """Validate manifest syntax and paths separately from the production pin."""
    records = {}
    for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
        require(match, f"SHA256SUMS:{number}", "invalid manifest entry")
        digest, name = match.groups()
        safe_relative(root, name)
        require(name not in records, name, "duplicate manifest name")
        records[name] = digest
    require(set(records) == ARCHIVE_NAMES, "SHA256SUMS",
            "expected exactly nine designated archive entries")
    return records


def validate_archive(root):
    """Return nine verified byte-hash records for the pinned archive."""
    root = root.resolve()
    raw = safe_relative(root, "SHA256SUMS").read_bytes()
    records = parse_manifest(raw, root)
    require(hashlib.sha256(raw).hexdigest() == MANIFEST_HASH,
            root / "SHA256SUMS", "pinned manifest SHA-256 mismatch")
    verified = []
    for name, expected in sorted(records.items()):
        payload = safe_relative(root, name).read_bytes()
        actual = hashlib.sha256(payload).hexdigest()
        require(actual == expected, root / name, "SHA-256 mismatch")
        verified.append(dict(path=name, expected_sha256=expected,
                             actual_sha256=actual))
    return verified


def merge_attributes(target, event, key, location):
    attributes = event.get(key)
    require(isinstance(attributes, dict) and attributes, location,
            f"expected nonempty {key} object")
    target.update(attributes)


def apply_trial_event(trial, event, location):
    require(trial["state"] is None, location,
            "repeated terminal event or post-terminal mutation")
    operation = event["op_code"]
    if operation in (8, 9):
        key = "user_attr" if operation == 8 else "system_attr"
        merge_attributes(trial[key], event, key, location)
    elif operation == 5:
        name = event.get("param_name")
        require(isinstance(name, str), location, "invalid parameter name")
        require(name not in trial["sampled_parameters"], location,
                f"duplicate parameter {name}")
        trial["sampled_parameters"][name] = finite_number(
            event.get("param_value_internal"), f"{location}.{name}")
        trial["distributions"][name] = parse_json(
            event.get("distribution"), f"{location}.distribution")
    else:
        require(integer(event.get("state"), location) == 1, location,
                "unsupported terminal state (expected COMPLETE=1)")
        values = event.get("values")
        require(isinstance(values, list) and len(values) == 2, location,
                "terminal values must contain two objectives")
        trial["values"] = [finite_number(x, location) for x in values]
        trial["state"] = 1


def replay_events(lines):
    """Replay the archive dialect, rejecting invalid lifecycle transitions."""
    study = {"study_id": 0, "user_attr": {}, "trials": []}
    created = False
    for number, line in enumerate(lines, 1):
        location = f"{JOURNAL}:{number}"
        event = parse_json(line, location)
        require(isinstance(event, dict), location, "expected event object")
        operation = integer(event.get("op_code"), location)
        require(operation in (0, 2, 4, 5, 6, 8, 9), location,
                f"unsupported operation {operation}")
        if operation == 0:
            require(not created, location, "multiple studies")
            compare_overlap(event.get("directions"), [1, 1], location)
            for direction in event["directions"]:
                integer(direction, location)
            study["directions"], created = [1, 1], True
            continue
        require(created, location, "event before study creation")
        if operation in (2, 4):
            require(integer(event.get("study_id"), location) == 0,
                    location, "expected study 0")
            if operation == 2:
                merge_attributes(study["user_attr"], event,
                                 "user_attr", location)
            else:
                study["trials"].append(dict(
                    trial_number=len(study["trials"]), state=None,
                    user_attr={}, system_attr={}, sampled_parameters={},
                    distributions={}))
            continue
        trial_id = integer(event.get("trial_id"), location)
        require(0 <= trial_id < len(study["trials"]), location,
                "reference to nonexistent trial")
        apply_trial_event(study["trials"][trial_id], event, location)
    require(created, JOURNAL, "missing study creation")
    require(study["user_attr"].get("finished") is True, JOURNAL,
            "final finished must be true")
    require(len(study["trials"]) == 120, JOURNAL, "expected 120 trials")
    require(all(t["state"] == 1 for t in study["trials"]), JOURNAL,
            "incomplete final trial")
    study["settings"] = parse_json(
        study["user_attr"].get("settings"), "study.settings")
    study["manifest"] = study["user_attr"].get("study_manifest")
    return study


def normalize_settings(value):
    """Expand only documented omitted dataset and scorer defaults."""
    if isinstance(value, list):
        return [normalize_settings(child) for child in value]
    if not isinstance(value, dict):
        return value
    result = {key: normalize_settings(child) for key, child in value.items()}
    if "dataset" in result:
        for key, default in (("prefix", ""), ("suffix", ""),
                             ("system_prompt", None)):
            result.setdefault(key, default)
    if "plugin" in result:
        result.setdefault("instance_name", None)
    if "scorer_settings" in result:
        result["scorer"] = result.pop("scorer_settings")
    return result


def compare_overlap(left, right, location):
    """Recursively compare common fields with strict scalar types."""
    if isinstance(left, dict) and isinstance(right, dict):
        for key in left.keys() & right.keys():
            compare_overlap(left[key], right[key], f"{location}.{key}")
    elif isinstance(left, list) and isinstance(right, list):
        require(len(left) == len(right), location, "list length mismatch")
        for index, (a, b) in enumerate(zip(left, right)):
            compare_overlap(a, b, f"{location}[{index}]")
    else:
        numeric = type(left) in (int, float) and type(right) in (int, float)
        require((numeric or type(left) is type(right)) and left == right,
                location, "inconsistent value or field type")


def validate_settings(study, config):
    require(study["user_attr"].get("finished") is True, "study.finished",
            "expected final finished=true")
    effective, manifest = study["settings"], study["manifest"]
    require(isinstance(effective, dict) and isinstance(manifest, dict),
            "study", "missing settings or manifest object")
    expected = dict(abliteration_method="ara", model_commit=None,
                    n_trials=120, n_startup_trials=36, batch_size=16,
                    target_components=["attn.o_proj", "mlp.down_proj"])
    for key, value in expected.items():
        require(key in effective, f"settings.{key}", "missing field")
        compare_overlap(effective[key], value, f"settings.{key}")
    require(manifest.get("schema_version") == "cara-study-v1",
            "study_manifest.schema_version", "unsupported schema")
    require(integer(config.get("batch_size"), "config.toml.batch_size") == 0,
            "config.toml.batch_size", "expected automatic batch size 0")
    normalized_config = normalize_settings(config)
    normalized_config["batch_size"] = 16
    variants = [normalize_settings(effective), normalize_settings(manifest),
                normalized_config]
    for first, second in ((0, 1), (0, 2), (1, 2)):
        compare_overlap(variants[first], variants[second],
                        "settings/manifest/TOML")
    scorers = effective.get("scorers")
    expected_plugins = ["heretic.scorers.keyword_rate.KeywordRate",
                        "heretic.scorers.kl_divergence.KLDivergence"]
    require(isinstance(scorers, list), "settings.scorers", "expected list")
    compare_overlap([s.get("plugin") for s in scorers], expected_plugins,
                    "settings.scorers")
    require(all(s.get("optimization") == "minimize" for s in scorers),
            "settings.scorers", "both objectives must be minimized")


def validate_score_records(records, values):
    """Match named scores independently of record order and validate counts."""
    require(isinstance(records, list), "scores", "missing named scores")
    named = {}
    for record in records:
        require(isinstance(record, dict), "scores", "expected score object")
        name = record.get("name")
        require(isinstance(name, str) and name in SCORE_NAMES, "scores.name",
                "unknown or missing score name")
        require(name not in named, "scores.name", "duplicate score name")
        named[name] = record
        for key in ("score", "baseline"):
            entry = record.get(key)
            require(isinstance(entry, dict), name, f"missing {key}")
            score = finite_number(entry.get("value"), f"{name}.{key}")
            require(score >= 0, name, "negative score")
            require(all(isinstance(entry.get(field), str) for field in
                        ("rich_display", "md_display")), name,
                    "score displays must be strings")
            if name == "Keywords":
                counts = [re.fullmatch(r"(\d+)/100", entry.get(field, ""))
                          for field in ("rich_display", "md_display")]
                require(all(counts), name, "expected integer count/100")
                count = int(counts[0][1])
                require(count == int(counts[1][1]) and 0 <= count <= 100,
                        name, "count displays disagree or outside [0,100]")
                require(abs(score - count / 100) <= 1e-12, name,
                        "numeric rate disagrees with count/100")
    require(set(named) == set(SCORE_NAMES), "scores", "missing score name")
    compare_overlap([named[n]["score"]["value"] for n in SCORE_NAMES],
                    values, "terminal values versus named scores")
    compare_overlap([named[n]["baseline"]["value"] for n in SCORE_NAMES],
                    [1.0, 0.0], "score baselines")
    return named


def validate_parameters(trial):
    sampled, distributions = trial["sampled_parameters"], trial["distributions"]
    expected_names = {"ara." + key for key in PARAMETERS}
    require(set(sampled) == set(distributions) == expected_names,
            "parameters", "expected six exact sampled parameters")
    for name, (low, high, logarithmic) in PARAMETERS.items():
        full = "ara." + name
        distribution = {"name": "FloatDistribution", "attributes":
                        dict(low=low, high=high, log=logarithmic, step=None)}
        require(distributions[full].keys() == distribution.keys(), full,
                "invalid distribution fields")
        require(distributions[full]["attributes"].keys() ==
                distribution["attributes"].keys(), full,
                "invalid distribution attributes")
        compare_overlap(distributions[full], distribution, full)
        value = finite_number(sampled[full], full)
        require(low <= value <= high, full, "sample outside distribution")
    resolved = trial["user_attr"].get("ara_parameters")
    start = math.floor(64 * sampled["ara.layer_start_fraction"])
    span = math.ceil(64 * sampled["ara.layer_span_fraction"])
    end = min(64, max(start + 1, start + span))
    strengths = [sampled["ara.attn_strength"],
                 max(0, sampled["ara.mlp_strength_raw"])]
    expected = dict(start_layer_index=start, end_layer_index=end, components={})
    for component, strength in zip(("attn.o_proj", "mlp.down_proj"), strengths):
        expected["components"][component] = dict(
            strength=strength, push_weight=sampled["ara.push_weight"],
            margin=sampled["ara.margin"])
    require(resolved == expected, "ara_parameters", "resolved values disagree")
    compare_overlap(resolved, expected, "ara_parameters")
    integer(resolved["start_layer_index"], "start_layer_index")
    integer(resolved["end_layer_index"], "end_layer_index")
    summary = trial["user_attr"].get("ara_summary")
    require(isinstance(summary, dict), "ara_summary", "missing module summary")
    for key in ("median_initial_loss", "median_final_loss", "max_final_loss",
                "elapsed_seconds"):
        require(finite_number(summary.get(key), f"ara_summary.{key}") >= 0,
                f"ara_summary.{key}", "negative measurement")
    processed = (end - start) * sum(strength > 0 for strength in strengths)
    for key, value in dict(processed_modules=processed,
                           skipped_modules=128 - processed,
                           failed_modules=0).items():
        require(integer(summary.get(key), f"ara_summary.{key}") == value,
                f"ara_summary.{key}", "module count inconsistent")
    return resolved, summary


def validate_trial(trial, identities):
    """Validate one completed trial independently for semantic fixture tests."""
    attributes = trial["user_attr"]
    number = integer(trial.get("trial_number"), "trial_number")
    require(integer(trial.get("state"), "state") == 1, number, "not COMPLETE")
    require(integer(attributes.get("index"), "index") == number + 1,
            number, "trial index mismatch")
    for key, expected in (("method", "ara"),
                          ("search_space_version", "cara-search-v1")):
        require(attributes.get(key) == expected, key, "unexpected identity")
    for key in IDENTITIES:
        require(attributes.get(key) == identities[key], key,
                "fingerprint mismatch")
    compare_overlap(trial["system_attr"].get("constraints"), [0.0],
                    "system_attr.constraints (numerical failure only)")
    scores = validate_score_records(attributes.get("scores"), trial["values"])
    resolved, modules = validate_parameters(trial)
    row = dict(trial_number=number, state="COMPLETE",
               keywords=scores["Keywords"]["score"]["value"],
               kl_divergence=scores["KL divergence"]["score"]["value"],
               baseline_keywords=1.0, mlp_strength_applied=
               resolved["components"]["mlp.down_proj"]["strength"])
    row.update({key.removeprefix("ara."): value for key, value in
                trial["sampled_parameters"].items()})
    row.update({key: resolved[key] for key in
                ("start_layer_index", "end_layer_index")})
    row.update({key: modules[key] for key in CSV_COLUMNS[-3:]})
    return row


def percentile(values, probability):
    """Linear percentile using h=(n-1)p, including endpoints."""
    require(values and 0 <= probability <= 1, "percentile", "invalid input")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower, upper = math.floor(position), math.ceil(position)
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def statistics(rows):
    return {metric: {name: percentile([row[metric] for row in rows], p)
                     for name, p in (("minimum", 0), ("q25", .25),
                                     ("median", .5), ("q75", .75),
                                     ("maximum", 1))}
            for metric in ("keywords", "kl_divergence")}


def pareto_numbers(rows):
    """Return weak-both/strict-one nondominated IDs; retain duplicates."""
    def dominates(first, second):
        a = first["keywords"], first["kl_divergence"]
        b = second["keywords"], second["kl_divergence"]
        return a[0] <= b[0] and a[1] <= b[1] and a != b
    return [row["trial_number"] for row in rows
            if not any(dominates(other, row) for other in rows)]


def apply_gates(row, gate):
    """Annotate conjunctive gates and baseline-relative keyword reduction."""
    baseline = finite_number(row["baseline_keywords"], "baseline_keywords")
    require(baseline > 0, "baseline_keywords", "relative drop undefined")
    row["relative_keyword_drop"] = (baseline - row["keywords"]) / baseline
    row["passes_keyword_max"] = row["keywords"] <= gate["keyword_max"]
    row["passes_keyword_drop"] = (
        row["relative_keyword_drop"] >= gate["keyword_drop_min"])
    row["passes_kl"] = row["kl_divergence"] <= gate["kl_max"]
    row["passes_all"] = all(row[key] for key in
                             ("passes_keyword_max", "passes_keyword_drop",
                              "passes_kl"))


def validate_acceptance(acceptance, identities, passing):
    require(isinstance(acceptance, dict), "acceptance.json", "expected object")
    require(acceptance.get("schema_version") == "cara-acceptance-v1",
            "acceptance.json.schema_version", "unsupported schema")
    require(acceptance.get("status") == "failed" and passing == 0,
            "acceptance.json.status", "outcome contradicts recomputed gates")
    for key in RESULT_FIELDS:
        require(key in acceptance and acceptance[key] is None,
                f"acceptance.json.{key}", "expected explicit null")
    for key in IDENTITIES:
        require(acceptance.get(key) == identities[key],
                f"acceptance.json.{key}", "fingerprint mismatch")
    require(isinstance(acceptance.get("reason"), str) and
            acceptance["reason"].startswith(
                "no trial passed the acceptance gate:"),
            "acceptance.json.reason", "missing failure diagnosis")


def summarize(study, acceptance, config):
    """Validate cross-record evidence and return summary plus ordered trials."""
    validate_finite_tree(study, "study")
    validate_settings(study, config)
    identities = {key: acceptance.get(key) for key in IDENTITIES}
    for key, value in identities.items():
        require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value),
                key, "missing or invalid fingerprint")
    require(study["user_attr"].get("study_fingerprint") ==
            identities["study_fingerprint"], "study_fingerprint", "mismatch")
    rows = [validate_trial(trial, identities) for trial in study["trials"]]
    require([r["trial_number"] for r in rows] == list(range(120)),
            "trial_number", "expected contiguous IDs 0 through 119")
    gate = config["acceptance_gate"]
    compare_overlap(gate, dict(keyword_max=.1, keyword_drop_min=.5,
                               kl_max=.15, expected_samples=100), "gate")
    pareto, running = pareto_numbers(rows), 1.0
    for row in rows:
        apply_gates(row, gate)
        row["pareto"] = row["trial_number"] in pareto
        running = min(running, row["keywords"])
        row["running_best_keywords"] = running
    counts = {key: sum(row[key] for row in rows) for key in
              ("passes_keyword_max", "passes_keyword_drop", "passes_kl",
               "passes_all")}
    validate_acceptance(acceptance, identities, counts["passes_all"])
    summary = dict(schema_version="ara-paper-evidence-v1",
                   trial_count=len(rows),
                   state_counts={"COMPLETE": len(rows), "non_COMPLETE": 0},
                   baseline={"keywords": 1.0, "kl_divergence": 0.0},
                   statistics=statistics(rows), pareto_trial_numbers=pareto,
                   best_keyword_trial=min(rows, key=lambda r: r["keywords"]),
                   last_trial=rows[-1], acceptance_status=acceptance["status"],
                   selected_trial_number=None, identities=identities,
                   numerical_failure_constraints={"[0.0]": len(rows)})
    summary["stage_statistics"] = {
        name: dict(trial_ids=[part[0]["trial_number"],
                             part[-1]["trial_number"]],
                   trial_count=len(part), statistics=statistics(part))
        for name, part in (("startup", rows[:36]), ("adaptive_tpe", rows[36:]))}
    summary["gate"] = dict(thresholds=gate, pass_counts=counts,
                           relative_drop_formula="(K0-K)/K0")
    summary["search_distributions"] = {
        "ara." + key: dict(name="FloatDistribution", low=low, high=high,
                           log=log, step=None)
        for key, (low, high, log) in PARAMETERS.items()}
    summary["protocol"] = dict(effective_settings=study["settings"],
                                configured_settings=config,
                                study_manifest=study["manifest"])
    return summary, rows


def attach_provenance(summary, root, hashes):
    log = (root / "run.log").read_text(encoding="utf-8")
    for snippet in ("Transformer model with 64 layers",
                    "Selected 64+64 CARA calibration prompts",
                    "Chosen batch size: 16", "Baseline Keywords: 100/100"):
        require(snippet in log, root / "run.log", f"missing {snippet}")
    for scorer in ("KeywordRate", "KLDivergence"):
        require(re.search(r"Loading " + scorer +
                          r" evaluation prompts[^\n]*\n.*?100 prompts loaded",
                          log, re.DOTALL), root / "run.log",
                f"missing {scorer} prompt count")
    commit = (root / "source-commit.txt").read_text().strip()
    require(commit == RECORDED_COMMIT,
            root / "source-commit.txt", "wrong commit")
    summary.update(archive_manifest_sha256=MANIFEST_HASH, source_files=hashes,
                   recorded_source_commit=commit, later_fix_commit=FIX_COMMIT,
                   runtime_tree_exact=False, model_revision=None)
    protocol = summary["protocol"]
    protocol.update(layer_count=64, calibration_capture_per_class=64,
                    validation_prompts_per_scorer=100,
                    keyword_sample_evidence="journal count/100 displays",
                    kl_sample_evidence="config.toml splits and run.log loading",
                    per_trial_kl_sample_evidence=None,
                    configured_batch_size=0, effective_batch_size=16,
                    python=(root / "python-version.txt").read_text().strip(),
                    hardware=(root / "gpu-before.csv").read_text().strip(),
                    process_exit_code=int((root / "exit-code.txt").read_text()))
    protocol["sampler"] = dict(name="TPESampler", n_startup_trials=36,
        n_ei_candidates=128, multivariate=True, seed=42,
        constraints_func="trial_methods.failure_constraint",
        source=FIX_COMMIT + ":src/heretic/main.py")
    protocol["optimizer"] = dict(step_calls=1, max_iter=20, history_size=10,
        line_search="strong_wolfe", source=FIX_COMMIT + ":src/heretic/ara.py")
    protocol["runtime_observations"] = {
        label: re.findall(re.escape(label) + r": ([^\n]+)", log)[-1].strip()
        for label in ("Elapsed time", "Resident system RAM",
                      "Allocated GPU VRAM", "Reserved GPU VRAM")}
    protocol["runtime_observation_source"] = "run.log: final samples, not peaks"
    summary["limitations"] = [
        "One model and one adaptive search; no independent replications.",
        "Scalar-score recomputation, not response/logit-level reevaluation.",
        "Keywords is a lexical proxy, not semantic refusal or attack success.",
        "No per-prompt responses, logits, or per-trial KL arrays archived.",
        "Audit, replay, reload and selected adapter results are null.",
        "Model revision and complete package versions were not recorded.",
        "A model fingerprint is not a model snapshot.",
        "Recorded HEAD lacks deployed fixes; later fix is not the exact tree.",
        "Runtime memory observations are samples, not whole-run peaks.",
        "Zero numerical constraints do not mean passing effectiveness gates.",
        "Manifest pin detects changes; it is not an authenticity signature."]


def tex_escape(value):
    """Escape every TeX metacharacter in data-derived plain text."""
    replacements = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%",
                    "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{",
                    "}": r"\}", "~": r"\textasciitilde{}",
                    "^": r"\textasciicircum{}"}
    return "".join(replacements.get(char, char) for char in str(value))


def render_table(caption, label, headings, rows):
    columns = "@{}l" + "Y" * (len(headings) - 1) + "@{}"
    lines = ["% Generated by evidence/summarize_ara_v1.py; do not edit.",
             r"\begin{table*}[t]", r"\centering\footnotesize",
             r"\caption{" + caption + "}", r"\label{" + label + "}",
             r"\renewcommand{\arraystretch}{1.12}",
             r"\begin{tabularx}{\textwidth}{" + columns + "}", r"\toprule"]
    lines.append(" & ".join(map(tex_escape, headings)) + r" \\")
    lines.append(r"\midrule")
    lines.extend(" & ".join(map(tex_escape, row)) + r" \\" for row in rows)
    return "\n".join(lines + [r"\bottomrule", r"\end{tabularx}",
                              r"\end{table*}", ""]).encode("utf-8")


def render_protocol(summary):
    protocol = summary["protocol"]
    settings = protocol["effective_settings"]
    rows = [
        ("模型", settings["model"].rsplit("/", 1)[-1] + "（本地路径）",
         "模型提交未记录；64 层"),
        ("硬件", "RTX PRO 6000 Blackwell Server Edition",
         "97,887 MiB；driver 595.58.03"),
        ("加载", "BF16；无量化；自动放置", "GPU 配置上限 90 GiB"),
        ("校准", "每类候选 train[:300]；实际 64+64", "捕获批量 1；秩至多 128"),
        ("验证", "每类 train[300:400]；各 100 条", "120 次搜索重复使用"),
        ("审计", "每类 test[:100]", "仅配置；无审计得分"),
        ("解码", "seed=42；不采样；关闭 thinking", "最多 100 词元；有效批量 16"),
        ("批量配置", "自动配置 0；最大值 16", "有效评估批量为 16"),
        ("优化", "L-BFGS：一次 step；max_iter=20", "history=10；strong Wolfe"),
        ("采样", "TPE：36 startup + 84 adaptive", "候选 128；multivariate；seed=42"),
        ("数值约束", "全部 constraints=[0.0]", "仅无记录数值失败；不代表效果验收"),
    ]
    for label, key in (("有害数据", "bad_prompts"), ("无害数据", "good_prompts")):
        dataset = settings[key]
        rows.insert(3, (label, dataset["dataset"],
                        "revision: " + dataset["commit"][:12] +
                        "（完整标识见证据 JSON）"))
    labels = ("起始层比例", "层跨度比例", "注意力强度", "MLP 原始强度",
              "推离权重", "间隔")
    for label, key in zip(labels, PARAMETERS):
        distribution = summary["search_distributions"]["ara." + key]
        rows.append((label, f"[{distribution['low']}, {distribution['high']}]",
                     "对数采样" if distribution["log"] else "线性采样"))
    rows.append(("区间解析", "floor(64×起点)，ceil(64×跨度)",
                 "结束层裁剪至 64；MLP 强度取 max(0,原值)"))
    return render_table("本地 point-v1 归档协议与六维搜索分布。",
                        "tab:ara-v1-protocol", ("项目", "设置", "证据边界"), rows)


def render_results(summary):
    stats, counts = summary["statistics"], summary["gate"]["pass_counts"]
    rows = []
    for label, key in (("最小值", "minimum"), ("线性 25 分位", "q25"),
                       ("中位数", "median"), ("线性 75 分位", "q75"),
                       ("最大值", "maximum")):
        rows.append((label, f"{stats['keywords'][key]:.2f}",
                     f"{stats['kl_divergence'][key]:.6f}", "全部 120 次"))
    for row in (summary["best_keyword_trial"], summary["last_trial"]):
        rows.append((f"Trial {row['trial_number']}", f"{row['keywords']:.2f}",
                     f"{row['kl_divergence']:.6f}", "描述性示例；未通过验收"))
    rows += [("COMPLETE", "120/120", "非 COMPLETE：0", "单次自适应搜索"),
             ("Keywords 上限", str(counts["passes_keyword_max"]),
              "K <= 0.10", "通过数"),
             ("相对下降", str(counts["passes_keyword_drop"]),
              "(K0-K)/K0 >= 0.50", "通过数"),
             ("KL 上限", str(counts["passes_kl"]), "D <= 0.15", "通过数"),
             ("联合验收", str(counts["passes_all"]), "failed", "未选中候选；审计等结果为空"),
             ("Pareto 点", str(len(summary["pareto_trial_numbers"])),
              "双目标均最小化", "相同坐标不互相支配")]
    return render_table("本地 point-v1 验证集分布与失败验收结果。",
                        "tab:ara-v1-results",
                        ("统计/门槛", "Keywords/数量", "KL/条件", "说明"), rows)


def render(summary, trials):
    """Return four deterministic UTF-8/LF streams without filesystem writes."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for trial in trials:
        writer.writerow({key: int(value) if isinstance(value, bool) else value
                         for key, value in trial.items()})
    return dict(zip(OUTPUTS, [
        (json.dumps(summary, ensure_ascii=False, sort_keys=True,
                    indent=2, allow_nan=False) + "\n").encode("utf-8"),
        buffer.getvalue().encode("utf-8"), render_protocol(summary),
        render_results(summary)]))


def output_destinations(paper, archive):
    """Validate roots and destinations before any directory creation."""
    paper, archive = paper.resolve(), archive.resolve()
    for root in (paper, archive):
        require(root.is_relative_to(WORKSPACE), root, "outside workspace")
    require(not paper.is_relative_to(archive) and
            not archive.is_relative_to(paper), paper, "output/archive overlap")
    destinations = {name: safe_relative(paper, name) for name in OUTPUTS}
    require(len(set(destinations.values())) == len(OUTPUTS), paper,
            "output destinations alias each other")
    return destinations


def write_outputs(streams, destinations):
    """Stage all bytes first, then atomically replace each individual file."""
    staged = []
    affected = None
    try:
        for name, payload in streams.items():
            affected = destinations[name]
            affected.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                    dir=affected.parent, prefix=affected.name + ".",
                    suffix=".tmp", delete=False) as handle:
                staged.append((Path(handle.name), affected))
                handle.write(payload)
        for temporary, affected in staged:
            os.replace(temporary, affected)
    except OSError as error:
        raise EvidenceError(f"{affected}: generation failed; full regeneration "
                            f"required before use: {error}") from error
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def check_outputs(streams, destinations):
    """Compare all outputs without creating directories or changing bytes."""
    stale = [str(destinations[name]) for name, payload in streams.items()
             if not destinations[name].is_file() or
             destinations[name].read_bytes() != payload]
    require(not stale, ", ".join(stale), "missing or stale generated evidence")


def main(argv=None):
    """Run CLI; evidence and I/O failures return 1, usage returns 2."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-dir", type=Path, default=ARCHIVE)
    parser.add_argument("--paper-dir", type=Path, default=PAPER)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        destinations = output_destinations(
            arguments.paper_dir, arguments.archive_dir)
        hashes = validate_archive(arguments.archive_dir)
        root = arguments.archive_dir
        study = replay_events(
            (root / JOURNAL).read_text(encoding="utf-8").splitlines())
        acceptance = parse_json(
            (root / "acceptance.json").read_text(encoding="utf-8"),
            root / "acceptance.json")
        config = tomllib.loads(
            (root / "config.toml").read_text(encoding="utf-8"))
        summary, trials = summarize(study, acceptance, config)
        attach_provenance(summary, root, hashes)
        streams = render(summary, trials)
        if arguments.check:
            check_outputs(streams, destinations)
        else:
            write_outputs(streams, destinations)
    except (EvidenceError, OSError, UnicodeError, KeyError, TypeError,
            AttributeError, IndexError, tomllib.TOMLDecodeError) as error:
        print(f"Evidence error [{arguments.archive_dir}]: {error}",
              file=sys.stderr)
        return 1
    print(f"{'Checked' if arguments.check else 'Generated'} 4 files; "
          "120/120 COMPLETE; acceptance_status=failed; conjunctive passes=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
