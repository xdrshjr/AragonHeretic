"""Evidence integrity regressions; all filesystem fixtures stay in the paper."""

import contextlib
import copy
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

import summarize_ara_v1 as evidence


FIXTURES = evidence.PAPER / "paper-output/revision-check"


class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FIXTURES.mkdir(parents=True, exist_ok=True)
        cls.lines = (evidence.ARCHIVE / evidence.JOURNAL).read_text(
            encoding="utf-8").splitlines()
        cls.events = [json.loads(line) for line in cls.lines]
        cls.study = evidence.replay_events(cls.lines)
        cls.acceptance = json.loads(
            (evidence.ARCHIVE / "acceptance.json").read_text(encoding="utf-8"))
        cls.config = tomllib.loads(
            (evidence.ARCHIVE / "config.toml").read_text(encoding="utf-8"))
        cls.identities = {
            key: cls.acceptance[key] for key in evidence.IDENTITIES}
        cls.hashes = evidence.validate_archive(evidence.ARCHIVE)
        cls.summary, cls.rows = evidence.summarize(
            cls.study, cls.acceptance, cls.config)
        evidence.attach_provenance(cls.summary, evidence.ARCHIVE, cls.hashes)
        cls.streams = evidence.render(cls.summary, cls.rows)

    @classmethod
    def tearDownClass(cls):
        if evidence.validate_archive(evidence.ARCHIVE) != cls.hashes:
            raise AssertionError("archive hashes changed during tests")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=FIXTURES,
                                                      prefix="evidence-test-")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def assert_replay_error(self, events, pattern):
        with self.assertRaisesRegex(evidence.EvidenceError, pattern):
            evidence.replay_events(json.dumps(event) for event in events)

    def invoke(self, *arguments):
        output = io.StringIO()
        with (contextlib.redirect_stdout(output),
              contextlib.redirect_stderr(output)):
            status = evidence.main(list(arguments))
        return status, output.getvalue()

    def test_full_archive_regression(self):
        self.assertEqual(len(self.hashes), 9)
        self.assertEqual(self.summary["trial_count"], 120)
        self.assertEqual(self.summary["state_counts"],
                         {"COMPLETE": 120, "non_COMPLETE": 0})
        self.assertEqual(self.summary["gate"]["pass_counts"], dict(
            passes_keyword_max=0, passes_keyword_drop=0, passes_kl=120,
            passes_all=0))
        self.assertEqual(self.summary["pareto_trial_numbers"],
                         [28, 40, 66, 79, 94, 100, 101, 112, 118, 119])
        best = self.summary["best_keyword_trial"]
        self.assertEqual(best["trial_number"], 66)
        self.assertEqual(best["keywords"], .54)
        self.assertAlmostEqual(best["kl_divergence"], .08997529745101929)
        self.assertEqual((best["start_layer_index"], best["end_layer_index"],
                          best["processed_modules"]), (16, 52, 72))
        self.assertAlmostEqual(best["relative_keyword_drop"], .46)
        self.assertEqual(self.rows[-1]["keywords"], .56)
        self.assertEqual(self.rows[-1]["kl_divergence"], .03191900998353958)
        expected = {
            "keywords": [.54, .88, .98, 1., 1.],
            "kl_divergence": [.0009053924586623907, .0036956561380065978,
                              .010353714693337679, .023271169513463974,
                              .11515633761882782]}
        for metric, values in expected.items():
            keys = ("minimum", "q25", "median", "q75", "maximum")
            for key, value in zip(keys, values):
                self.assertAlmostEqual(self.summary["statistics"][metric][key],
                                       value, delta=1e-12)
        self.assertIsNone(
            self.summary["protocol"]["per_trial_kl_sample_evidence"])
        self.assertEqual(self.summary["acceptance_status"], "failed")

    def test_hand_calculated_percentiles_and_duplicate_pareto(self):
        self.assertEqual(evidence.percentile([0, 10, 20, 30], .25), 7.5)
        self.assertEqual(evidence.percentile([3], .75), 3)
        pairs = [(1, 1), (1, 1), (0, 2), (2, 0), (2, 2), (1, 2)]
        rows = [dict(trial_number=n, keywords=k, kl_divergence=d)
                for n, (k, d) in enumerate(pairs)]
        self.assertEqual(evidence.pareto_numbers(rows), [0, 1, 2, 3])

    def test_gate_equality_and_relative_drop_are_independent(self):
        row = dict(keywords=.1, kl_divergence=.15, baseline_keywords=.2)
        evidence.apply_gates(row, dict(keyword_max=.1, keyword_drop_min=.5,
                                       kl_max=.15))
        self.assertTrue(row["passes_all"])
        self.assertEqual(row["relative_keyword_drop"], .5)
        self.assertNotEqual(row["relative_keyword_drop"], .2 - .1)
        row["baseline_keywords"] = 0
        with self.assertRaisesRegex(evidence.EvidenceError, "undefined"):
            evidence.apply_gates(row, self.config["acceptance_gate"])

    def test_reordered_named_scores_preserve_summary(self):
        study = copy.deepcopy(self.study)
        for trial in study["trials"]:
            trial["user_attr"]["scores"].reverse()
        summary, rows = evidence.summarize(study, self.acceptance, self.config)
        self.assertEqual(rows, self.rows)
        self.assertEqual(summary["statistics"], self.summary["statistics"])

    def test_missing_duplicate_names_and_terminal_mismatch(self):
        for change, message in (
                (lambda t: t["user_attr"]["scores"].pop(), "missing score"),
                (lambda t: t["user_attr"]["scores"].append(
                    t["user_attr"]["scores"][0]), "duplicate score"),
                (lambda t: t["values"].__setitem__(0, .01), "terminal values")):
            with self.subTest(message=message):
                trial = copy.deepcopy(self.study["trials"][0])
                change(trial)
                with self.assertRaisesRegex(evidence.EvidenceError, message):
                    evidence.validate_trial(trial, self.identities)

    def test_count_displays_and_baseline_are_validated(self):
        for field, value in (("rich_display", "97/99"),
                             ("md_display", "96/100"),
                             ("md_display", 97), ("value", .98),
                             ("rich_display", "101/100")):
            trial = copy.deepcopy(self.study["trials"][0])
            trial["user_attr"]["scores"][0]["score"][field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises(evidence.EvidenceError):
                    evidence.validate_trial(trial, self.identities)
        trial = copy.deepcopy(self.study["trials"][0])
        trial["user_attr"]["scores"][1]["baseline"]["value"] = .1
        with self.assertRaisesRegex(evidence.EvidenceError, "baselines"):
            evidence.validate_trial(trial, self.identities)

    def test_fingerprints_and_trial_identity(self):
        changes = [(key, "0" * 64) for key in evidence.IDENTITIES]
        changes += [("index", 2), ("index", True), ("method", "other"),
                    ("search_space_version", "cara-search-v2")]
        for key, value in changes:
            with self.subTest(key=key):
                trial = copy.deepcopy(self.study["trials"][0])
                trial["user_attr"][key] = value
                with self.assertRaises(evidence.EvidenceError):
                    evidence.validate_trial(trial, self.identities)

    def test_distribution_sample_resolution_and_module_corruption(self):
        changes = [
            lambda t: t["distributions"]["ara.margin"]["attributes"].update(
                log=False),
            lambda t: t["sampled_parameters"].update({"ara.margin": 10}),
            lambda t: t["sampled_parameters"].update(
                {"ara.margin": float("nan")}),
            lambda t: t["sampled_parameters"].update({"ara.margin": True}),
            lambda t: t["user_attr"]["ara_parameters"].update(
                start_layer_index=0),
            lambda t: t["user_attr"]["ara_parameters"]["components"][
                "mlp.down_proj"].update(strength=0),
            lambda t: t["user_attr"]["ara_summary"].update(processed_modules=1),
            lambda t: t["user_attr"]["ara_summary"].update(skipped_modules=-1),
            lambda t: t["user_attr"]["ara_summary"].update(failed_modules=True),
            lambda t: t["user_attr"]["ara_summary"].update(
                elapsed_seconds="17"),
            lambda t: t["system_attr"].update(constraints=[1.0]),
        ]
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                trial = copy.deepcopy(self.study["trials"][0])
                change(trial)
                with self.assertRaises(evidence.EvidenceError):
                    evidence.validate_trial(trial, self.identities)

    def test_boolean_cannot_impersonate_zero_applied_mlp_strength(self):
        trial = copy.deepcopy(next(t for t in self.study["trials"]
                                   if t["sampled_parameters"][
                                       "ara.mlp_strength_raw"] < 0))
        trial["user_attr"]["ara_parameters"]["components"][
            "mlp.down_proj"]["strength"] = False
        with self.assertRaisesRegex(evidence.EvidenceError, "field type"):
            evidence.validate_trial(trial, self.identities)

    def test_malformed_duplicate_and_nonfinite_json(self):
        for text in ("{bad", '{"x":1,"x":2}', '{"x":NaN}',
                     '{"x":Infinity}', '{"x":-Infinity}', '{"x":1e999}'):
            with self.subTest(text=text):
                with self.assertRaises(evidence.EvidenceError):
                    evidence.parse_json(text, "fixture:7")
        event = copy.deepcopy(self.events[1])
        event["user_attr"]["settings"] = '{"seed":42,"seed":1}'
        events = copy.deepcopy(self.events)
        events[1] = event
        self.assert_replay_error(events, "duplicate JSON")

    def test_lifecycle_corruption_uses_small_event_fixtures(self):
        creation = next(e for e in self.events if e["op_code"] == 4)
        parameter = next(e for e in self.events if e["op_code"] == 5)
        terminal = next(e for e in self.events if e["op_code"] == 6)
        prefix = [self.events[0], creation]
        cases = [([{"op_code": 3}], "unsupported operation"),
                 ([parameter], "before study creation"),
                 ([self.events[0], parameter], "nonexistent trial"),
                 (prefix + [terminal, terminal], "repeated terminal"),
                 (prefix + [terminal, parameter], "post-terminal"),
                 (prefix + [parameter, parameter], "duplicate parameter"),
                 ([self.events[0], self.events[0]], "multiple studies"),
                 ([{"op_code": True}], "expected integer")]
        for events, pattern in cases:
            with self.subTest(pattern=pattern):
                self.assert_replay_error(events, pattern)
        for key, value in (("state", 3), ("state", True), ("values", []),
                           ("trial_id", 1)):
            corrupt = dict(terminal, **{key: value})
            with self.assertRaises(evidence.EvidenceError):
                evidence.replay_events(map(json.dumps, prefix + [corrupt]))

    def test_finished_trial_count_and_id_gaps(self):
        events = copy.deepcopy(self.events)
        events[-1]["user_attr"]["finished"] = False
        self.assert_replay_error(events, "finished")
        self.assert_replay_error(self.events[:5] + [self.events[-1]],
                                 "120 trials")
        study = copy.deepcopy(self.study)
        study["trials"][-1]["trial_number"] = 120
        study["trials"][-1]["user_attr"]["index"] = 121
        with self.assertRaisesRegex(evidence.EvidenceError, "contiguous IDs"):
            evidence.summarize(study, self.acceptance, self.config)

    def test_settings_disagree_and_documented_defaults_are_normalized(self):
        for area, key, value in (("settings", "seed", 7),
                                 ("manifest", "ara_lora_rank", 64),
                                 ("settings", "batch_size", 8)):
            study = copy.deepcopy(self.study)
            study[area][key] = value
            with self.subTest(area=area, key=key):
                with self.assertRaises(evidence.EvidenceError):
                    evidence.validate_settings(study, self.config)
        study = copy.deepcopy(self.study)
        study["settings"]["scorer"]["KeywordRate"]["prompts"]["prefix"] = "x"
        with self.assertRaisesRegex(evidence.EvidenceError, "prefix"):
            evidence.validate_settings(study, self.config)
        evidence.validate_settings(self.study, self.config)

    def test_acceptance_status_and_every_null_result(self):
        for key, value in [("status", "passed"), ("schema_version", "v2")] + [
                (key, {}) for key in evidence.RESULT_FIELDS]:
            report = copy.deepcopy(self.acceptance)
            report[key] = value
            with self.subTest(key=key):
                with self.assertRaises(evidence.EvidenceError):
                    evidence.validate_acceptance(report, self.identities, 0)
        with self.assertRaisesRegex(evidence.EvidenceError, "contradicts"):
            evidence.validate_acceptance(self.acceptance, self.identities, 1)
        report = dict(self.acceptance)
        del report["audit_scores"]
        with self.assertRaisesRegex(evidence.EvidenceError, "explicit null"):
            evidence.validate_acceptance(report, self.identities, 0)

    def test_manifest_pin_truncation_replacement_and_content_hash(self):
        archive = self.root / "archive"
        shutil.copytree(evidence.ARCHIVE, archive)
        manifest = archive / "SHA256SUMS"
        original = manifest.read_bytes()
        for payload in (original[:-90], original.replace(b"41c4", b"01c4")):
            manifest.write_bytes(payload)
            with self.assertRaises(evidence.EvidenceError):
                evidence.validate_archive(archive)
        manifest.write_bytes(original)
        (archive / "acceptance.json").write_bytes(b"{}")
        with self.assertRaisesRegex(evidence.EvidenceError, "acceptance.json"):
            evidence.validate_archive(archive)

    def test_manifest_paths_duplicates_and_missing_journal(self):
        raw = (evidence.ARCHIVE / "SHA256SUMS").read_bytes()
        for name in ("../escape", "/absolute", "C:/drive", "a/../../escape"):
            corrupt = raw.replace(b"acceptance.json", name.encode())
            with self.subTest(name=name):
                with self.assertRaises(evidence.EvidenceError):
                    evidence.parse_manifest(corrupt, self.root)
        with self.assertRaisesRegex(evidence.EvidenceError, "duplicate"):
            evidence.parse_manifest(
                raw + raw.splitlines(keepends=True)[0], self.root)
        archive = self.root / "archive"
        shutil.copytree(evidence.ARCHIVE, archive)
        (archive / evidence.JOURNAL).unlink()
        status, output = self.invoke("--archive-dir", str(archive),
                                     "--paper-dir", str(self.root / "paper"))
        self.assertEqual(status, 1)
        self.assertIn(evidence.JOURNAL.split("/")[-1], output)
        self.assertFalse((self.root / "paper").exists())

    def test_output_roots_and_symlink_escape(self):
        for paper in (evidence.ARCHIVE, evidence.ARCHIVE / "out",
                      evidence.ARCHIVE.parent, evidence.WORKSPACE.parent):
            with self.subTest(paper=paper):
                with self.assertRaises(evidence.EvidenceError):
                    evidence.output_destinations(paper, evidence.ARCHIVE)
        output = self.root / "paper"
        output.mkdir()
        try:
            (output / "evidence").symlink_to(self.root,
                                             target_is_directory=True)
        except OSError as error:
            self.skipTest(f"OS cannot create symlink: {error}")
        with self.assertRaisesRegex(evidence.EvidenceError, "symlink"):
            evidence.output_destinations(output, evidence.ARCHIVE)
        archive = self.root / "archive"
        shutil.copytree(evidence.ARCHIVE, archive)
        manifest = archive / "SHA256SUMS"
        manifest.unlink()
        manifest.symlink_to(evidence.ARCHIVE / "SHA256SUMS")
        with self.assertRaisesRegex(evidence.EvidenceError, "symlink"):
            evidence.validate_archive(archive)

    def test_two_generations_check_and_stale_read_only(self):
        paper = self.root / "paper"
        arguments = ("--paper-dir", str(paper))
        self.assertEqual(self.invoke(*arguments)[0], 0)
        first = {name: (paper / name).read_bytes() for name in evidence.OUTPUTS}
        self.assertEqual(self.invoke(*arguments)[0], 0)
        self.assertEqual(first, {name: (paper / name).read_bytes()
                                 for name in evidence.OUTPUTS})
        self.assertEqual(self.invoke(*arguments, "--check")[0], 0)
        stale = paper / evidence.OUTPUTS[2]
        stale.write_bytes(b"stale\n")
        self.assertEqual(self.invoke(*arguments, "--check")[0], 1)
        self.assertEqual(stale.read_bytes(), b"stale\n")
        missing = self.root / "missing"
        self.assertEqual(
            self.invoke("--paper-dir", str(missing), "--check")[0], 1)
        self.assertFalse(missing.exists())
        self.assertFalse(list(paper.rglob("*.tmp")))
        self.assertTrue(all(b"\r" not in payload for payload in first.values()))

    def test_cli_default_paths_work_from_another_directory(self):
        result = subprocess.run([sys.executable, "-B",
                                 str(Path(evidence.__file__)),
                                 "--paper-dir", str(self.root / "paper")],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_staging_failure_preserves_old_bytes_and_removes_temporaries(self):
        destinations = evidence.output_destinations(self.root, evidence.ARCHIVE)
        evidence.write_outputs(self.streams, destinations)
        create = evidence.tempfile.NamedTemporaryFile
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("fixture staging failure")
            return create(*args, **kwargs)

        changed = {name: payload + b"changed"
                   for name, payload in self.streams.items()}
        with mock.patch.object(evidence.tempfile, "NamedTemporaryFile",
                               fail_second):
            with self.assertRaisesRegex(evidence.EvidenceError,
                                         "generation failed"):
                evidence.write_outputs(changed, destinations)
        self.assertEqual(
            {name: path.read_bytes() for name, path in destinations.items()},
            self.streams)
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_failed_replacement_exits_one_and_check_requires_regeneration(self):
        paper = self.root / "paper"
        destinations = evidence.output_destinations(paper, evidence.ARCHIVE)
        old = {name: b"old\n" for name in evidence.OUTPUTS}
        evidence.write_outputs(old, destinations)
        replace = evidence.os.replace
        calls = 0

        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("fixture promotion failure")
            return replace(source, target)

        with mock.patch.object(evidence.os, "replace", fail_second):
            status, message = self.invoke("--paper-dir", str(paper))
        self.assertEqual(status, 1)
        self.assertIn("full regeneration", message)
        self.assertIn("ara-v1-trials.csv", message)
        self.assertNotIn("Generated 4", message)
        self.assertFalse(list(paper.rglob("*.tmp")))
        self.assertEqual(
            self.invoke("--paper-dir", str(paper), "--check")[0], 1)
        self.assertEqual(self.invoke("--paper-dir", str(paper))[0], 0)
        self.assertEqual(
            self.invoke("--paper-dir", str(paper), "--check")[0], 0)

    def test_corrupt_archive_never_replaces_existing_outputs(self):
        archive, paper = self.root / "archive", self.root / "paper"
        shutil.copytree(evidence.ARCHIVE, archive)
        destinations = evidence.output_destinations(paper, archive)
        old = {name: b"preserve\n" for name in evidence.OUTPUTS}
        evidence.write_outputs(old, destinations)
        (archive / "acceptance.json").write_bytes(b"malformed")
        self.assertEqual(self.invoke("--archive-dir", str(archive),
                                     "--paper-dir", str(paper))[0], 1)
        self.assertEqual(
            {name: path.read_bytes() for name, path in destinations.items()},
            old)

    def test_tex_escaping_and_json_full_precision(self):
        escaped = evidence.tex_escape("a_b%&{x}\\#$~^")
        self.assertIn(r"a\_b\%\&\{x\}", escaped)
        self.assertIn(r"\textbackslash{}", escaped)
        decoded = json.loads(self.streams[evidence.OUTPUTS[0]])
        self.assertEqual(decoded["best_keyword_trial"]["kl_divergence"],
                         .08997529745101929)
        self.assertEqual(evidence.render(decoded, self.rows), self.streams)


if __name__ == "__main__":
    unittest.main()
