# SPDX-License-Identifier: AGPL-3.0-or-later
"""Continuous refusal-prefix log-odds scorer."""

from __future__ import annotations

import torch
from pydantic import BaseModel, Field, PositiveInt, model_validator

from heretic.config import DatasetSpecification
from heretic.continuation_scores import logmeanexp
from heretic.ara_trajectory import TrajectoryNonFiniteError
from heretic.plugin import Context
from heretic.protocol_data import fingerprint_dataset
from heretic.scorer import Score, Scorer
from heretic.utils import print


class Settings(BaseModel):
    """Configuration for deterministic refusal-prefix scoring."""

    prompts: DatasetSpecification
    refusal_prefixes: list[str] = Field(min_length=1)
    answer_prefixes: list[str] = Field(min_length=1)
    batch_tokens: PositiveInt

    @model_validator(mode="after")
    def validate_text_prefixes(self) -> "Settings":
        """Reject empty and duplicate normalized prefix text."""
        normalized_groups = []
        for group in (self.refusal_prefixes, self.answer_prefixes):
            normalized = [prefix.strip() for prefix in group]
            if any(not prefix for prefix in normalized):
                raise ValueError("prefixes must not be empty")
            if len(normalized) != len(set(normalized)):
                raise ValueError("prefixes must be unique after stripping")
            normalized_groups.append(set(normalized))
        if normalized_groups[0] & normalized_groups[1]:
            raise ValueError("refusal and answer prefixes must be disjoint")
        return self


class RefusalLogOdds(Scorer):
    """Mean refusal versus direct-answer continuation log probability ratio."""

    settings: Settings

    @property
    def reproducible(self) -> bool:
        """Return whether this scorer is deterministic for a pinned model."""
        return True

    @property
    def score_name(self) -> str:
        """Return the stable external score name."""
        return "Refusal log-odds"

    def init(self, ctx: Context) -> None:
        """Load validation prompts and validate token-level prefix identities."""
        print()
        print(
            "Loading RefusalLogOdds evaluation prompts from "
            f"[bold]{self.settings.prompts.dataset}[/]..."
        )
        self.prompts = ctx.load_prompts(self.settings.prompts)
        self.refusal_prefixes, self.answer_prefixes = ctx.validate_prefix_groups(
            self.settings.refusal_prefixes,
            self.settings.answer_prefixes,
        )
        self.dataset_fingerprint = fingerprint_dataset(
            self.settings.prompts,
            self.prompts,
        )
        print(f"* [bold]{len(self.prompts)}[/] prompts loaded")

    def get_score(self, ctx: Context) -> Score:
        """Evaluate raw length-normalized refusal log-odds."""
        prefixes = self.refusal_prefixes + self.answer_prefixes
        values = ctx.get_continuation_logprobs(
            self.prompts,
            prefixes,
            self.settings.batch_tokens,
        )
        refusal_count = len(self.refusal_prefixes)
        refusal = logmeanexp(values[:, :refusal_count], dim=1)
        answer = logmeanexp(values[:, refusal_count:], dim=1)
        score = (refusal - answer).mean()
        if not torch.isfinite(score):
            raise TrajectoryNonFiniteError(
                "refusal-score", "Refusal log-odds score is non-finite"
            )
        value = float(score)
        return Score(
            value=value,
            rich_display=f"{value:.4f}",
            md_display=f"{value:.4f}",
            sample_count=len(self.prompts),
            dataset_fingerprint=self.dataset_fingerprint,
        )
