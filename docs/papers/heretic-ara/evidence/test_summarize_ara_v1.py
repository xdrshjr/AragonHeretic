"""Behavioral tests for scientific data integrity and fail-closed outputs."""

import copy
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.dont_write_bytecode = True
import ara_v1_data as archive  # noqa: E402
import summarize_ara_v1 as generator  # noqa: E402

TEST_ROOT = generator.DEFAULT_PAPER / "build/test-tmp"


def _operation(opcode, **fields):
    return {"op_code": opcode, "worker_id": "test-worker", **fields}


def _score(value, count):
    return {"value": value, "md_display": count, "rich_display": count}


def _journal_records():
    return [
        _operation(0, study_name="test", directions=[1, 1]),
        _operation(2, study_id=0, user_attr={"first": 1}),
        _operation(2, study_id=0, user_attr={"second": 2}),
        _operation(4, study_id=0, datetime_start="2026-09-04T00:00:00"),
        _operation(8, trial_id=0, user_attr={"index": 1}),
        _operation(8, trial_id=0, user_attr={"scores": [
            {"name": "KL divergence", "score": _score(.04, "0.0400"),
             "baseline": _score(0, "0 (by definition)")},
            {"name": "Keywords", "score": _score(.4, "4/10"),
             "baseline": _score(.8, "8/10")},
        ]}),
        _operation(9, trial_id=0, system_attr={"constraints": [0.0]}),
        _operation(9, trial_id=0, system_attr={"sampler_note": "retained"}),
        _operation(6, trial_id=0, state=1, values=[.4, .04],
                   datetime_complete="2026-09-04T00:00:01"),
    ]


def _payload(records):
    return ("\n".join(json.dumps(row) for row in records) + "\n").encode()


def _replay(records):
    return archive.replay_journal(_payload(records))


def _gate():
    return {"expected_samples": 10, "keyword_max": .5,
            "keyword_drop_min": .45, "kl_max": .15}


class JournalTests(unittest.TestCase):
    """Small inputs test format independently of fixed archive identities."""

    def test_names_line_numbers_baseline_and_system_merge(self):
        payload = b"\n" + _payload(_journal_records()).replace(b"\n", b"\r\n")
        study, trials = archive.replay_journal(payload)
        self.assertEqual(study, {"first": 1, "second": 2})
        trial = archive.make_trial(trials[0], _gate(), {})
        self.assertEqual(trial["score_lines"], [7])
        self.assertEqual(trial["terminal_line"], 10)
        self.assertEqual(trial["keyword_count"], 4)
        self.assertEqual(trial["sample_count"], 10)
        self.assertEqual(trial["baseline_keywords"], .8)
        self.assertEqual(trial["absolute_drop"], .4)
        self.assertEqual(trial["relative_drop"], .5)
        self.assertFalse(trial["numerical_gate_pass"])
        self.assertEqual(trial["system_attrs"],
                         {"constraints": [0.0], "sampler_note": "retained"})

    def test_cr_refresh_fragments_share_physical_line(self):
        result = list(archive.log_fragments(b"first\rrefresh\nnext\r\n"))
        self.assertEqual([(r["line"], r["fragment"]) for r in result],
                         [(1, 1), (1, 2), (2, 1)])
        self.assertEqual(result[1]["text"], "refresh")

    def test_creation_order_requires_display_index(self):
        records = _journal_records()
        records[4]["user_attr"]["index"] = 2
        with self.assertRaisesRegex(archive.InputError, "display index"):
            _replay(records)

    def test_two_trials_follow_creation_order(self):
        records = _journal_records()
        second = copy.deepcopy(records[3:])
        for record in second:
            if "trial_id" in record:
                record["trial_id"] = 1
            if "index" in record.get("user_attr", {}):
                record["user_attr"]["index"] = 2
        trials = _replay(records + second)[1]
        self.assertEqual([trial["trial_number"] for trial in trials], [0, 1])

    def test_parameter_distribution_is_preserved_and_unknown_shape_fails(self):
        record = _operation(5, trial_id=0, param_name="ara.margin",
                            param_value_internal=.5, distribution=json.dumps({
                                "name": "FloatDistribution", "attributes": {
                                    "step": None, "low": .1, "high": 1.0,
                                    "log": True,
                                },
                            }))
        records = _journal_records()
        raw = _replay(records[:-1] + [record] + records[-1:])[1][0]
        self.assertEqual(raw["raw_params"]["ara.margin"], .5)
        self.assertTrue(raw["distributions"]["ara.margin"]["attributes"]["log"])
        record["distribution"] = json.dumps({"name": "Unknown"})
        with self.assertRaises(archive.InputError):
            _replay(records[:-1] + [record] + records[-1:])

    def test_terminal_objectives_must_agree_with_named_scores(self):
        records = _journal_records()
        records[-1]["values"] = [.04, .4]
        raw = _replay(records)[1][0]
        with self.assertRaisesRegex(archive.InputError, "disagree"):
            archive.make_trial(raw, _gate(), {})

    def test_baseline_and_trial_denominators_are_checked(self):
        for role in ("score", "baseline"):
            with self.subTest(role=role):
                records = _journal_records()
                records[5]["user_attr"]["scores"][1][role] = _score(.4, "8/20")
                raw = _replay(records)[1][0]
                with self.assertRaisesRegex(archive.InputError, "denominator"):
                    archive.make_trial(raw, _gate(), {})

    def test_fingerprint_conflict_is_not_silently_merged(self):
        raw = _replay(_journal_records())[1][0]
        raw["user_attrs"]["model_fingerprint"] = "wrong"
        with self.assertRaisesRegex(archive.InputError, "fingerprint conflict"):
            archive.make_trial(raw, _gate(), {"model_fingerprint": "expected"})

    def test_failed_trial_preserves_null_scores(self):
        records = _journal_records()
        records[-1].update(state=3, values=None)
        raw = _replay(records)[1][0]
        result = archive.make_trial(raw, _gate(), {})
        self.assertEqual(result["state"], "FAIL")
        self.assertIsNone(result["keywords"])
        self.assertIsNone(result["relative_drop"])
        with self.assertRaisesRegex(archive.InputError, "COMPLETE"):
            archive.summarize_trials([result])

    def test_incomplete_and_duplicate_terminal_are_rejected(self):
        records = _journal_records()
        for broken in (records[:-1], records + [records[-1]]):
            with self.subTest(length=len(broken)):
                with self.assertRaisesRegex(archive.InputError, "terminal"):
                    _replay(broken)

    def test_uncreated_trial_wrong_study_and_unknown_op_are_rejected(self):
        for record in (
            _operation(8, trial_id=1, user_attr={}),
            _operation(2, study_id=1, user_attr={}),
            _operation(7, trial_id=0, step=1, intermediate_value=1),
        ):
            with self.subTest(record=record):
                with self.assertRaises(archive.InputError):
                    _replay(_journal_records()[:1] + [record])

    def test_bad_json_utf8_nonfinite_and_duplicate_fields_fail(self):
        for payload in (b"{broken}\n", b"\xff\n", b'{"x":NaN}',
                        b'{"x":Infinity}', b'{"x":1e999}',
                        b'{"op_code":0,"op_code":2}'):
            with self.subTest(payload=payload):
                with self.assertRaises(archive.InputError):
                    archive.replay_journal(payload)
        with self.assertRaises(archive.InputError):
            archive.parse_json('{"x":1}'.encode("utf-16"))

    def test_missing_complete_values_or_scores_fail(self):
        for missing in ("values", "scores"):
            records = _journal_records()
            if missing == "values":
                records[-1]["values"] = None
            else:
                records[5]["user_attr"].pop("scores")
            raw = _replay(records)[1][0]
            with self.assertRaises(archive.InputError):
                archive.make_trial(raw, _gate(), {})


class StatisticsTests(unittest.TestCase):
    def test_pareto_keeps_equal_points_and_rejects_weakly_worse(self):
        points = [(.3, .1), (.3, .1), (.4, .1), (.2, .3), (.8, .8)]
        trials = [{"trial_number": index, "keywords": k, "kl": kl}
                  for index, (k, kl) in enumerate(points)]
        self.assertEqual(archive.pareto_numbers(trials), [0, 1, 3])

    def test_quantiles_interpolate_sorted_values(self):
        self.assertEqual(archive.linear_quantile([8, 0, 4, 2], .25), 1.5)
        self.assertEqual(archive.linear_quantile([8], .5), 8)
        with self.assertRaises(archive.InputError):
            archive.linear_quantile([], .5)

    def test_zero_baseline_and_nonunit_baseline_drops_differ(self):
        self.assertEqual(archive.calculate_drops(0, .2), (-.2, None))
        self.assertEqual(archive.calculate_drops(.8, .4), (.4, .5))

    def test_tied_best_is_ordered_by_kl_then_trial_number(self):
        raw = _replay(_journal_records())[1][0]
        trials = [archive.make_trial(raw, _gate(), {}) for _ in range(3)]
        for number, trial in enumerate(trials):
            trial["trial_number"] = number
        trials[0]["kl"] = .1
        result = archive.summarize_trials(list(reversed(trials)))
        self.assertEqual(result["best_keyword_trial"], 1)
        self.assertEqual(result["min_kl_trial"], 1)

    def test_settings_reject_semantic_conflict(self):
        config = {"seed": 42, "scorer": {"KeywordRate": {"x": 1}}}
        effective = copy.deepcopy(config)
        effective["scorer"]["KeywordRate"]["x"] = 2
        with self.assertRaisesRegex(archive.InputError, "scorer.KeywordRate.x"):
            archive.compare_settings(config, effective, {})

    def test_settings_map_manifest_scorer_settings(self):
        config = {"scorer": {"KeywordRate": {"x": 1}}}
        manifest = {"scorer_settings": {"KeywordRate": {"x": 2}}}
        with self.assertRaisesRegex(archive.InputError, "study_manifest"):
            archive.compare_settings(config, config, manifest)


class SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = archive.load_summary()

    def test_original_nine_hashes_and_versioned_sources(self):
        sources = self.summary["source_records"]
        original = [s for s in sources if s["archive_member"]]
        self.assertEqual(len(original), 9)
        self.assertTrue(all(s["sha256"] == s["expected_sha256"]
                            for s in original))
        blobs = [s for s in sources if s["source_kind"] == "git_blob"]
        self.assertEqual(len(blobs), 4)
        self.assertTrue(all(s["revision"] == archive.FIX_REVISION
                            for s in blobs))
        self.assertEqual(len({s["source_id"] for s in sources}), len(sources))
        parameters = self.summary["trials"][66]["resolved_params"]
        self.assertEqual([parameters["start_layer_index"],
                          parameters["end_layer_index"]], [16, 52])

    def test_exact_snapshot_statistics(self):
        summary = self.summary
        self.assertEqual(summary["state_counts"], {"COMPLETE": 120})
        self.assertEqual(summary["numerical_gate_count"], 0)
        self.assertEqual(summary["gate_counts"],
                         {"keywords": 0, "kl": 120, "joint": 0})
        self.assertEqual(summary["best_keyword_trial"], 66)
        self.assertEqual(summary["min_kl_trial"], 40)
        self.assertEqual(summary["last_trial"], 119)
        self.assertEqual(summary["pareto_trial_numbers"],
                         [28, 40, 66, 79, 94, 100, 101, 112, 118, 119])
        for number, keywords, kl in (
            (40, 1.0, .0009053924586623907),
            (66, .54, .08997529745101929),
            (119, .56, .03191900998353958),
        ):
            trial = summary["trials"][number]
            self.assertAlmostEqual(trial["keywords"], keywords, delta=1e-12)
            self.assertAlmostEqual(trial["kl"], kl, delta=1e-12)

    def test_raw_quantiles_and_phase_means(self):
        expected_kl = [.0009053924586623907, .0036956561380065978,
                       .010353714693337679, .023271169513463974,
                       .11515633761882782]
        observed = self.summary["quantiles"]["kl"]
        for value, expected in zip(observed, expected_kl):
            self.assertAlmostEqual(value, expected, delta=1e-12)
        self.assertEqual(self.summary["quantiles"]["keywords"],
                         [.54, .88, .98, 1.0, 1.0])
        means = [.9866666666666667, .8779545454545454, .91975]
        for phase, mean in zip(self.summary["phase_statistics"], means):
            self.assertAlmostEqual(phase["mean"], mean, delta=1e-12)
        self.assertEqual(self.summary["phase_statistics"][1]["median"], .905)

    def test_display_precision_and_null_acceptance(self):
        tables = generator.render_outputs(self.summary)
        results = tables["tables/ara-v1-results.tex"].decode()
        distribution = tables["tables/ara-v1-distribution.tex"].decode()
        for token in ("0.000905", "0.089975", "0.031919", "54/100"):
            self.assertIn(token, results)
        self.assertIn("0.905 & 0.878", distribution)
        self.assertEqual(self.summary["acceptance_status"], "failed")
        for key in ("selected_trial_number", "audit_scores", "reload_scores",
                    "validation_replay_scores", "artifact_hashes"):
            self.assertIsNone(self.summary[key])

    def test_missing_historical_blob_is_not_replaced_by_worktree(self):
        with self.assertRaisesRegex(archive.InputError, "missing historical"):
            archive.read_git_blob("0" * 40, "src/heretic/ara.py")

    def test_unrelated_commit_does_not_change_recomputed_artifacts(self):
        run = subprocess.run

        def changed_head(command, **kwargs):
            if command == ["git", "rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(command, 0, b"f" * 40)
            return run(command, **kwargs)

        with mock.patch.object(subprocess, "run", changed_head):
            after_commit = archive.load_summary()
        self.assertEqual(generator.render_outputs(self.summary),
                         generator.render_outputs(after_commit))


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        area = tempfile.TemporaryDirectory(prefix="checkout-", dir=TEST_ROOT)
        self.addCleanup(area.cleanup)
        self.paper = Path(area.name)
        original = generator.DEFAULT_PAPER
        shutil.copyfile(original / "paper.tex", self.paper / "paper.tex")
        for name in ("sections", "tables"):
            shutil.copytree(original / name, self.paper / name)

    def test_unused_historical_chapters_do_not_enter_document(self):
        import verify_paper as verifier

        before = verifier._tex_document(self.paper)
        (self.paper / "sections/08-conclusion.tex").write_text(
            r"\label{sec:conclusion}\cite{removed-bib-key}", encoding="utf-8",
        )
        self.assertEqual(verifier._tex_document(self.paper), before)

    def test_duplicate_included_labels_are_still_rejected(self):
        import verify_paper as verifier

        path = self.paper / "paper.tex"
        content = path.read_text(encoding="utf-8")
        path.write_text(content.replace(
            r"\end{document}",
            r"\input{sections/07-conclusion}" + "\n" + r"\end{document}",
        ), encoding="utf-8")
        with self.assertRaisesRegex(verifier.VerificationError, "Duplicate"):
            verifier._tex_document(self.paper)

    def test_missing_included_chapter_is_rejected(self):
        import verify_paper as verifier

        (self.paper / "sections/07-conclusion.tex").unlink()
        with self.assertRaisesRegex(verifier.VerificationError,
                                    "Missing TeX input"):
            verifier._tex_document(self.paper)

    def test_cyclic_inclusion_is_rejected_with_file_location(self):
        import verify_paper as verifier

        path = self.paper / "sections/07-conclusion.tex"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(r"\input{paper}")
        with self.assertRaisesRegex(verifier.VerificationError,
                                    "Cyclic TeX input: paper"):
            verifier._tex_document(self.paper)

    def test_git_keeps_actual_binary_asset_bytes(self):
        for name in ("ara-architecture.png", "ara-v1-search.png",
                     "ara-v1-search.pdf"):
            path = generator.DEFAULT_PAPER / "figures" / name
            relative = path.relative_to(archive.REPO_ROOT).as_posix()
            hashes = [subprocess.run(
                ["git", "hash-object", option, relative],
                cwd=archive.REPO_ROOT, capture_output=True, check=True,
                timeout=30,
            ).stdout for option in ("--no-filters", "--path=" + relative)]
            with self.subTest(asset=name):
                self.assertEqual(hashes[0], hashes[1])


class OutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        cls.area = tempfile.TemporaryDirectory(prefix="output ", dir=TEST_ROOT)
        cls.paper = Path(cls.area.name) / "paper with spaces"
        cls.summary = archive.load_summary()
        generator.generate_outputs(cls.summary, cls.paper)

    @classmethod
    def tearDownClass(cls):
        cls.area.cleanup()

    def _run_check(self, source=archive.DEFAULT_SOURCE):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return generator.main(["--source", str(source), "--paper-dir",
                                   str(self.paper), "--check"])

    def test_good_check_is_read_only_without_matplotlib_import(self):
        paths = [p for p in self.paper.rglob("*") if p.is_file()]
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        with mock.patch.object(generator, "_render_figures",
                               side_effect=AssertionError("must not redraw")):
            self.assertEqual(self._run_check(), 0)
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        self.assertEqual(before, after)

    def test_modified_table_pdf_png_and_csv_return_three_without_writes(self):
        for name in ("tables/ara-v1-results.tex", *generator.FIGURE_OUTPUTS,
                     "evidence/ara-v1-trials.csv"):
            with self.subTest(name=name):
                path = self.paper / name
                original = path.read_bytes()
                changed = (original[:50] if name.endswith("png")
                           else original + b"x")
                path.write_bytes(changed)
                try:
                    self.assertEqual(self._run_check(), 3)
                    self.assertEqual(path.read_bytes(), changed)
                finally:
                    path.write_bytes(original)

    def test_input_bad_hash_missing_file_and_manifest_escape_return_two(self):
        source = Path(self.area.name) / "source copy"
        shutil.copytree(archive.DEFAULT_SOURCE, source)
        target = source / "acceptance.json"
        original = target.read_bytes()
        target.write_bytes(original + b" ")
        self.assertEqual(self._run_check(source), 2)
        before = {name: (self.paper / name).read_bytes()
                  for name in generator.TEXT_OUTPUTS}
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = generator.main(["--source", str(source), "--paper-dir",
                                     str(self.paper)])
        self.assertEqual(status, 2)
        self.assertEqual(before, {name: (self.paper / name).read_bytes()
                                  for name in generator.TEXT_OUTPUTS})
        target.unlink()
        self.assertEqual(self._run_check(source), 2)
        target.write_bytes(original)
        manifest = source / "SHA256SUMS"
        manifest.write_text("0" * 64 + " *../escape\n", encoding="utf-8")
        self.assertEqual(self._run_check(source), 2)

    def test_overlap_and_outside_workspace_are_rejected_before_creation(self):
        for output in (archive.DEFAULT_SOURCE, archive.DEFAULT_SOURCE.parent,
                       archive.DEFAULT_SOURCE / "derived",
                       archive.REPO_ROOT.parent):
            with self.subTest(output=output):
                with self.assertRaises(archive.InputError):
                    generator.validate_paths(archive.DEFAULT_SOURCE, output)

    def test_mid_generation_failure_leaves_check_failing(self):
        original = (self.paper / "tables/ara-v1-results.tex").read_bytes()
        (self.paper / "tables/ara-v1-results.tex").write_bytes(b"stale")
        real_replace = generator.os.replace
        calls = 0

        def interrupted_replace(source, destination):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("simulated write interruption")
            real_replace(source, destination)

        try:
            replacement = mock.patch.object(
                generator.os, "replace", interrupted_replace,
            )
            with replacement:
                with self.assertRaisesRegex(generator.OutputError,
                                            "interruption"):
                    generator.generate_outputs(self.summary, self.paper)
            self.assertEqual(self._run_check(), 3)
        finally:
            (self.paper / "tables/ara-v1-results.tex").write_bytes(original)


class ReviewBindingTests(unittest.TestCase):
    def test_review_must_bind_current_pdf_and_every_page(self):
        import verify_paper

        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="review-", dir=TEST_ROOT)
        with temporary as area:
            paper = Path(area)
            (paper / "evidence").mkdir()
            (paper / "build/pages").mkdir(parents=True)
            for number in (1, 2):
                (paper / f"build/pages/page-{number}.png").write_bytes(b"page")
            current = {"pdf_sha256": "a" * 64, "page_count": 2}
            review = {**current, "status": "passed", "render_dpi": 120,
                      "reviewed_pages": [1, 2],
                      "reviewed_at_utc": "2026-09-08T00:00:00Z"}
            self._write_review(paper, review)
            self.assertEqual(len(verify_paper.check_review(paper, current)), 64)
            for change in ({"pdf_sha256": "b" * 64},
                           {"reviewed_pages": [1]},
                           {"reviewed_pages": [1, 1, 2]},
                           {"status": "pending_review"}):
                with self.subTest(change=change):
                    self._write_review(paper, review | change)
                    with self.assertRaises(verify_paper.VerificationError):
                        verify_paper.check_review(paper, current)

    def _write_review(self, paper, record):
        (paper / "evidence/review.md").write_text(
            "```json\n" + json.dumps(record) + "\n```\n", encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
