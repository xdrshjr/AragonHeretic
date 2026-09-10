# SPDX-License-Identifier: AGPL-3.0-or-later

import unittest

from heretic.ara_config import DatasetSpecification
from heretic.protocol_data import (
    DatasetMetadata,
    ProtocolDataError,
    RoleDataset,
    materialize_prompt_bundle,
    parse_absolute_split,
    preflight_audit_metadata,
    validate_role_overlaps,
    validate_role_sizes,
)
from heretic.utils import Prompt


def spec(split: str, dataset: str = "org/data") -> DatasetSpecification:
    return DatasetSpecification(
        dataset=dataset,
        commit="a" * 40,
        split=split,
        column="text",
    )


class ProtocolDataTests(unittest.TestCase):
    def test_fixed_416_row_revision_rejects_old_validation_before_load(self):
        metadata = DatasetMetadata(
            {"train": 416, "test": 104}, frozenset({"text"})
        )
        with self.assertRaisesRegex(ProtocolDataError, "416"):
            preflight_audit_metadata(spec("train[400:500]"), lambda _: metadata)
        self.assertTrue(
            preflight_audit_metadata(spec("train[300:400]"), lambda _: metadata)
        )

    def test_training_role_reader_rejects_audit_without_opening_file(self):
        from heretic.research_protocol import load_role_body
        from pathlib import Path

        with self.assertRaisesRegex(ValueError, "禁止读取审计"):
            load_role_body({}, Path("does-not-exist"), "research-audit.bad")

    def test_cross_role_normalized_content_leak_is_rejected(self):
        from heretic.research_protocol import _validate_identity_isolation

        rows = []
        for side in ("good", "bad"):
            rows.append(
                {
                    "prompt_id": side,
                    "source": side,
                    "revision": "rev",
                    "row_index": 0,
                    "scenario_group_id": side,
                    "normalized_text_hash": "same-normalized-content",
                }
            )
        with self.assertRaisesRegex(ValueError, "跨角色泄漏"):
            _validate_identity_isolation(
                {
                    "fit.good": {"prompts": rows[:1]},
                    "development.bad": {"prompts": rows[1:]},
                }
            )

    def test_only_absolute_finite_slices_are_accepted(self) -> None:
        self.assertEqual(parse_absolute_split("train[400:500]").size, 100)
        for value in ("train", "train[:10%]", "train[10:]", "train[3:3]"):
            with (
                self.subTest(value=value),
                self.assertRaises(ProtocolDataError),
            ):
                parse_absolute_split(value)

    def test_shared_validation_is_allowed_but_cross_role_overlap_is_not(
        self,
    ) -> None:
        validation = spec("train[400:500]")
        validate_role_overlaps(
            [
                RoleDataset("validation_harmful", validation),
                RoleDataset("validation_harmful", validation.model_copy()),
            ]
        )
        with self.assertRaisesRegex(ProtocolDataError, "overlap"):
            validate_role_overlaps(
                [
                    RoleDataset("calibration", spec("train[:450]")),
                    RoleDataset("validation", validation),
                ]
            )

    def test_protocol_roles_require_preregistered_row_counts(self) -> None:
        datasets = [RoleDataset("validation", spec("train[400:500]"))]
        validate_role_sizes(datasets, {"validation": 100})
        with self.assertRaisesRegex(ProtocolDataError, "expected 99"):
            validate_role_sizes(datasets, {"validation": 99})

    def test_metadata_preflight_does_not_materialize_rows(self) -> None:
        calls = []

        def metadata_loader(item):
            calls.append(item.split)
            return DatasetMetadata({"test": 200}, frozenset({"text"}))

        fingerprint = preflight_audit_metadata(
            spec("test[:100]"),
            metadata_loader,
        )
        self.assertEqual(calls, ["test[:100]"])
        self.assertEqual(len(fingerprint), 64)

    def test_materialization_checks_count_and_empty_values(self) -> None:
        bundle = materialize_prompt_bundle(
            "validation",
            spec("train[:2]"),
            lambda item: [Prompt("s", "one"), Prompt("s", "two")],
        )
        self.assertEqual(bundle.row_indices, (0, 1))
        with self.assertRaisesRegex(ProtocolDataError, "empty"):
            materialize_prompt_bundle(
                "validation",
                spec("train[:1]"),
                lambda item: [Prompt("s", " ")],
            )


if __name__ == "__main__":
    unittest.main()
