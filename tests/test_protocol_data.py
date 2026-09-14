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


class NewProfileContractTests(unittest.TestCase):
    def test_new_protocol_consumers_reject_legacy_or_unknown_version(self):
        import tempfile
        from pathlib import Path
        from heretic.ara_research_schema import digest
        from heretic.research_protocol import (
            _validate_new_protocol,
            build_research_manifest,
        )

        with tempfile.TemporaryDirectory() as directory:
            config = _new_preparation_fixture(Path(directory))
            protocol = build_research_manifest(config)
            for version in ("cara-research-protocol-v3", "unknown", None):
                changed = {**protocol, "schema_version": version}
                changed["protocol_hash"] = digest(
                    {
                        key: value
                        for key, value in changed.items()
                        if key != "protocol_hash"
                    }
                )
                with self.subTest(version=version):
                    with self.assertRaisesRegex(ValueError, "版本"):
                        _validate_new_protocol(changed)

    def test_target_rejects_scorer_precision_and_role_content_drift(self):
        import copy
        import tempfile
        from pathlib import Path
        from heretic.research_protocol import (
            _validate_new_protocol,
            build_research_manifest,
        )

        with tempfile.TemporaryDirectory() as directory:
            config = _new_preparation_fixture(Path(directory))
            protocol = build_research_manifest(config)
            _validate_new_protocol(protocol)
            mutations = {
                "judge_identity": {"rubric_hash": "changed"},
                "quantization": "none",
                "dtype": "float32",
            }
            for key, value in mutations.items():
                with self.subTest(key=key), self.assertRaises(ValueError):
                    _validate_new_protocol({**protocol, key: value})
            for field in ("body_hash", "prompts", "source"):
                changed = copy.deepcopy(protocol)
                bundle = changed["roles"]["development.bad"]
                if field == "prompts":
                    bundle[field][0]["normalized_text_hash"] = "changed"
                elif field == "source":
                    bundle[field]["sha256"] = "changed"
                else:
                    bundle[field] = "changed"
                with self.subTest(field=field), self.assertRaises(ValueError):
                    _validate_new_protocol(changed)

    def test_target_profiles_cannot_change_development_body_identity(self):
        from test_ara_pilot import contract_fixture
        from heretic.ara_research_schema import validate_target_contract

        contract = contract_fixture()
        contract["role_identities"]["full-calibration"]["development.bad"][
            "body_hash"
        ] = "changed"
        with self.assertRaisesRegex(ValueError, "正文|身份"):
            validate_target_contract(contract)

    def test_hardware_model_mismatch_is_rejected_before_model_load(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from heretic.research_protocol import _validate_hardware_identity

        config = SimpleNamespace(
            ara_runtime_guard=SimpleNamespace(
                required_target_devices=["cuda:0"]
            )
        )
        with patch("torch.cuda.get_device_name", return_value="另一型号"):
            with self.assertRaisesRegex(ValueError, "GPU 型号"):
                _validate_hardware_identity(
                    config, {"hardware": {"device_names": ["预注册型号"]}}
                )

    def test_full_mapping_keeps_development_and_requires_192_candidates(self):
        from test_ara_pilot import contract_fixture
        from heretic.ara_research_schema import validate_target_contract

        contract = contract_fixture()
        validate_target_contract(contract)
        full = contract["role_mappings"]["full-calibration"]
        full["development.bad"] = list(reversed(full["development.bad"]))
        with self.assertRaisesRegex(ValueError, "保持不变"):
            validate_target_contract(contract)
        contract = contract_fixture()
        contract["role_mappings"]["full-calibration"]["fit.good"].pop()
        with self.assertRaisesRegex(ValueError, "数量"):
            validate_target_contract(contract)

    def test_full_calibration_is_not_treated_as_eight_sample_smoke(self):
        from heretic.research_protocol import _is_smoke, _validate_bundle_counts

        protocol = {"pilot": True, "pilot_profile": "full-calibration"}
        self.assertFalse(_is_smoke(protocol))
        bundle = {
            "prompts": [{}] * 192,
            "candidate_count": 192,
            "selected_count": 96,
        }
        _validate_bundle_counts("fit.good", bundle, _is_smoke(protocol))
        with self.assertRaises(ValueError):
            _validate_bundle_counts("fit.good", bundle, True)


def _new_role_sources(root, contract):
    from heretic.ara_research_schema import digest, file_digest, write_json
    from heretic.research_protocol import _prepare_rows

    sources, identities = {}, {}
    for role, ids in contract["role_mappings"]["smoke"].items():
        if role.startswith("_"):
            continue
        rows = [
            {
                "prompt_id": key,
                "scenario_group_id": key,
                "primary_in_group": True,
                "source": role,
                "revision": "fixed-revision",
                "row_index": index,
                "language": "en",
                "category": "fixture",
                "text": key + " body",
                "system": "fixed system",
            }
            for index, key in enumerate(ids)
        ]
        path = root / (role + ".json")
        write_json(path, {"rows": rows})
        source = {
            "path": path.name,
            "sha256": file_digest(path),
            "license": "test",
        }
        prompts, body = _prepare_rows(role, source, root)
        identities[role] = {
            "prompts_hash": digest(prompts),
            "body_hash": digest(body),
            "source_hash": digest(
                {k: v for k, v in source.items() if k != "path"}
            ),
        }
        sources[role] = source
    return sources, identities


def _new_target_fixture(root):
    import copy
    from test_ara_pilot import contract_fixture

    contract = contract_fixture()
    sources, identities = _new_role_sources(root, contract)
    contract["quantization"], contract["dtype"] = "bnb_4bit", "bfloat16"
    contract["model_identity"]["file_hashes"] = {"weights.bin": "test"}
    contract["tokenizer_identity"]["file_hashes"] = {"tokenizer.json": "test"}
    contract["scorer_identity"] = {"rubric_hash": "frozen"}
    contract["generation_profiles"] = {
        "fit": 8,
        "keywords": 100,
        "semantic": 256,
        "sequence_kl": 32,
        "chat_template_kwargs": {"enable_thinking": False},
        "generation_kwargs": {"do_sample": False},
    }
    contract["role_identities"]["smoke"] = identities
    for role in identities:
        if role.startswith(("development.", "mechanism-development.")):
            contract["role_identities"]["full-calibration"][role] = (
                copy.deepcopy(identities[role])
            )
    return contract, sources


def _new_preparation_fixture(root):
    from heretic.ara_research_schema import digest

    contract, sources = _new_target_fixture(root)
    return {
        **{
            key: contract[key]
            for key in (
                "model_identity",
                "tokenizer_identity",
                "source_files",
                "package_versions",
                "generation_profiles",
                "initialization_sources",
                "role_layout",
                "quantization",
                "dtype",
                "seeds",
            )
        },
        "schema_version": "cara-research-protocol-v3.1",
        "protocol_id": "identity-fixture",
        "pilot": True,
        "pilot_profile": "smoke",
        "target_execution_contract": contract,
        "target_execution_hash": digest(contract),
        "source_tree_hash": digest(contract["source_files"]),
        "input_root": str(root),
        "output": str(root / "protocol" / "protocol.json"),
        "required_level": "statistical_extreme",
        "phase_budgets": {},
        "replay_weight_comparison": "effective-update-v1",
        "judge_identity": contract["scorer_identity"],
        "roles": sources,
        "ability_tasks": {},
        "audit_history": {},
        "sampling": {
            "sampling_frame_hash": "test",
            "population": "test",
            "seed": 42,
            "inclusion_rules": "all",
            "exclusion_rules": "none",
            "grouping_rule": "one",
            "inference_scope": "benchmark_only",
        },
    }


if __name__ == "__main__":
    unittest.main()
