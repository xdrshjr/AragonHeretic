# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026  Philipp Emanuel Weidmann <pew@worldwidemann.com> + contributors

from dataclasses import dataclass
from typing import Any

from optuna.study import StudyDirection
from pydantic import BaseModel

from .config import DatasetSpecification, ScorerConfig, Settings
from .model import Model
from .plugin import get_plugin_namespace, is_builtin_plugin, load_plugin
from .scorer import Context, Score, Scorer
from .utils import deep_merge_dicts, parse_study_direction, print
from .utils import Prompt


@dataclass
class ScorerEntry:
    scorer: Scorer
    name: str
    config: ScorerConfig


class Evaluator:
    """
    Manages evaluation of the model using configured scorer plugins.

    Loads scorers, establishes baseline scores, and runs scorers during optimization.
    """

    settings: Settings
    model: Model

    def __init__(
        self,
        settings: Settings,
        model: Model,
        prompt_cache: dict[str, list[Prompt]] | None = None,
    ):
        self.settings = settings
        self.model = model
        self._scorer_entries: list[ScorerEntry] = []
        self._prompt_cache = dict(prompt_cache or {})

        print()
        print("Loading and initializing scorers...")
        self._load_and_init_scorers()

        # Establish baseline scores (pre-abliteration).
        self.baseline_scores = self.get_baseline_scores()
        self._print_baseline()

    def _create_scorer_entry(
        self,
        config: ScorerConfig,
        scorer_keys: set[str],
    ) -> ScorerEntry:
        scorer_cls = load_plugin(name=config.plugin, base_class=Scorer)
        scorer_cls.validate_contract()
        suffix = f"- {config.instance_name}" if config.instance_name else ""
        print(f"* Loaded: [bold]{scorer_cls.__name__} {suffix}[/bold]")
        instance_name = config.instance_name or None
        raw = self._get_scorer_settings_raw(
            scorer_cls=scorer_cls, instance_name=instance_name
        )
        scorer_settings: BaseModel | None = scorer_cls.validate_settings(raw)
        scorer = scorer_cls(heretic_settings=self.settings, settings=scorer_settings)
        key = (
            scorer_cls.__name__
            if not instance_name
            else f"{scorer_cls.__name__}_{instance_name}"
        )
        if key in scorer_keys:
            raise ValueError(
                f"Duplicate scorer instance name: {key}. "
                "Give each instance a unique `instance_name`."
            )
        scorer_keys.add(key)
        name = (
            f"{scorer.score_name} - {instance_name}"
            if instance_name
            else scorer.score_name
        )
        return ScorerEntry(scorer=scorer, config=config, name=name)

    def _load_and_init_scorers(self) -> None:
        """Load configured scorer plugins and run their initialization hooks."""
        if not self.settings.scorers:
            raise ValueError("No scorers configured. Set 'scorers' in config.toml")
        scorer_keys: set[str] = set()
        for config in self.settings.scorers:
            self._scorer_entries.append(self._create_scorer_entry(config, scorer_keys))
        ctx = Context(
            settings=self.settings,
            model=self.model,
            prompt_cache=self._prompt_cache,
        )

        for entry in self._scorer_entries:
            entry.scorer.init(ctx)

    def _print_baseline(self) -> None:
        """Print baseline scores summary."""
        for name, score in self.baseline_scores:
            print(f"* Baseline {name}: [bold]{score.rich_display}[/]")

    def get_dataset_specifications(self) -> list[DatasetSpecification]:
        """
        Collect the dataset specifications declared in the settings of all
        loaded scorers.
        """
        specifications = []
        for entry in self._scorer_entries:
            if entry.scorer.settings is None:
                continue
            for value in dict(entry.scorer.settings).values():
                if isinstance(value, DatasetSpecification):
                    specifications.append(value)
        return specifications

    def _get_scorer_settings_raw(
        self, *, scorer_cls: type[Scorer], instance_name: str | None
    ) -> dict[str, Any]:
        """
        Build the raw settings dict for a scorer class and optional instance.

        Config rules:
        - Base settings live in `[scorer.ClassName]` (applies to all instances).
        - Instance overrides live in `[scorer.ClassName_<instance_name>]` (preferred).
        - Only merge/validate keys that exist in the scorer Settings schema.
        """
        settings_model = scorer_cls.get_settings_model()
        if settings_model is None:
            # No settings schema: nothing to merge/validate.
            return {}

        class_name = scorer_cls.__name__

        namespaces = [f"scorer.{class_name}"]
        if instance_name:
            namespaces.append(f"scorer.{class_name}_{instance_name}")

        merged_settings: dict[str, Any] = {}
        allowed_keys = set(settings_model.model_fields.keys())

        for namespace in namespaces:
            raw_table = get_plugin_namespace(self.settings.model_extra, namespace)
            filtered = {k: v for k, v in raw_table.items() if k in allowed_keys}
            merged_settings = deep_merge_dicts(merged_settings, filtered)

        return merged_settings

    def all_scorers_reproducible(self) -> bool:
        """
        Returns True if all scorers are reproducible,
        False if not.
        """
        return all(entry.scorer.reproducible for entry in self._scorer_entries)

    def all_scorers_builtin(self) -> bool:
        """
        Returns True if all scorers are built-in,
        i.e included in Heretic by default.
        """
        return all(
            is_builtin_plugin(entry.config.plugin) for entry in self._scorer_entries
        )

    def get_scores(self) -> list[tuple[str, Score]]:
        """
        Run all scorers and return their scores and names

        Returns:
            List of `Score` from each scorer and its name.
        """
        ctx = Context(settings=self.settings, model=self.model)
        return [
            (entry.name, entry.scorer.get_score(ctx)) for entry in self._scorer_entries
        ]

    def get_baseline_scores(self) -> list[tuple[str, Score]]:
        """
        Run all scorers and return their baseline scores and names

        Returns:
            List of `Score` from each scorer and its name.
        """
        ctx = Context(settings=self.settings, model=self.model)
        return [
            (entry.name, entry.scorer.get_baseline_score(ctx))
            for entry in self._scorer_entries
        ]

    def get_paired_score_records(
        self, scores: list[tuple[str, Score]]
    ) -> list[dict[str, Any]]:
        """
        Pair each trial score with its baseline into one serializable record.

        `scores` (from `get_scores()`) and `self.baseline_scores` are both ordered
        by `_scorer_entries`, so they align positionally.
        """
        if len(scores) != len(self.baseline_scores):
            raise ValueError("Score and baseline counts differ")
        records: list[dict[str, Any]] = []
        for (name, score), (baseline_name, baseline) in zip(
            scores, self.baseline_scores
        ):
            if name != baseline_name:
                raise ValueError(
                    f"Score/baseline order mismatch: {name!r} != {baseline_name!r}"
                )
            for field in ("sample_count", "dataset_fingerprint"):
                if getattr(score, field) != getattr(baseline, field):
                    raise ValueError(f"Score/baseline {field} mismatch for {name}")
            score_payload = dict(score.__dict__)
            baseline_payload = dict(baseline.__dict__)
            for payload in (score_payload, baseline_payload):
                if payload.get("sample_count") is None:
                    payload.pop("sample_count", None)
                if payload.get("dataset_fingerprint") is None:
                    payload.pop("dataset_fingerprint", None)
            records.append(
                {
                    "name": name,
                    "score": score_payload,
                    "baseline": baseline_payload,
                }
            )
        return records

    def _objective_entries(self) -> list[ScorerEntry]:
        """
        Scorer entries that participate in optimization, in canonical order.
        Single source of truth for which scorers are objectives and in what
        order. Every objective-derived list (names, directions, values) is built
        from this so they stay positionally aligned: Optuna matches the objective
        values returned each trial to the study `directions` by index, so a length
        or order mismatch here would silently corrupt the optimization.
        """
        return [
            entry
            for entry in self._scorer_entries
            if parse_study_direction(entry.config.optimization)
            != StudyDirection.NOT_SET
        ]

    def get_objective_names(self) -> list[str]:
        """Return objective names for scores used in optimization."""
        return [entry.name for entry in self._objective_entries()]

    def get_objective_values(
        self, scores: list[tuple[str, Score]]
    ) -> tuple[float, ...]:
        """
        Extract objective values as a tuple for Optuna.

        Ordered by `_objective_entries()` so the result aligns by index with
        `get_objective_names()` and `get_objective_directions()`.
        """
        score_by_name = {name: score for name, score in scores}
        return tuple(
            score_by_name[entry.name].value for entry in self._objective_entries()
        )

    def get_objective_directions(self) -> list[StudyDirection]:
        """Get optimization directions for objectives."""
        return [
            parse_study_direction(entry.config.optimization)
            for entry in self._objective_entries()
        ]
