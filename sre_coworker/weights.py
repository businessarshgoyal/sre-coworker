"""Tunable triage parameters, kept in a YAML file so `sre-coworker tune` can re-fit them.

Everything that is a judgment call in triage.py (how much recency vs. keyword overlap
matters, dedupe thresholds, stop-words, synonyms) lives here. Code holds the structure of
the reasoning; this file holds the numbers, and the regression cases hold the ground truth.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

DEFAULT_STOP_WORDS = [
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "with",
    "after", "before", "from", "by", "at", "be", "this", "that", "it", "as",
    "error", "errors", "failing", "failed", "failure", "spike", "high", "rate", "alert",
]  # fmt: skip

DEFAULT_SYNONYMS: dict[str, list[str]] = {
    "5xx": ["500", "502", "503", "504", "server-error"],
    "timeout": ["timeouts", "timed-out", "deadline", "slow", "latency"],
    "retry": ["retries", "retrying", "backoff"],
    "db": ["database", "postgres", "mysql", "sql"],
    "pool": ["connection-pool", "connections"],
    "oom": ["memory", "out-of-memory", "oomkilled"],
}


class Weights(BaseModel):
    # deploy correlation
    recency_weight: float = 0.6
    keyword_weight: float = 0.4
    keyword_saturation: int = 3
    service_bonus: float = 0.15
    unrelated_penalty: float = 0.5
    revert_penalty: float = 0.4
    suspect_threshold: float = 0.2
    # confidence from top deploy
    deploy_confidence_base: float = 0.4
    deploy_confidence_slope: float = 0.5
    unrelated_confidence: float = 0.4
    revert_confidence: float = 0.4
    no_match_confidence: float = 0.3
    # runbooks
    runbook_service_weight: float = 0.5
    runbook_keyword_weight: float = 0.2
    runbook_threshold: float = 0.3
    # known-issue dedupe
    dupe_overlap_weight: float = 0.25
    dupe_same_service_weight: float = 0.3
    dupe_threshold: float = 0.55
    dupe_confidence: float = 0.9
    # vocabulary
    stop_words: list[str] = Field(default_factory=lambda: list(DEFAULT_STOP_WORDS))
    synonyms: dict[str, list[str]] = Field(default_factory=lambda: dict(DEFAULT_SYNONYMS))

    def stop_set(self) -> frozenset[str]:
        return frozenset(self.stop_words)

    def canonical(self) -> dict[str, str]:
        """variant -> canonical term, built from `synonyms`."""
        out: dict[str, str] = {}
        for canon, variants in self.synonyms.items():
            for v in variants:
                out[v] = canon
        return out


# numeric fields the tuner may perturb, with (min, max) bounds
TUNABLE: dict[str, tuple[float, float]] = {
    "recency_weight": (0.2, 0.9),
    "keyword_weight": (0.1, 0.8),
    "service_bonus": (0.0, 0.4),
    "unrelated_penalty": (0.1, 0.9),
    "revert_penalty": (0.1, 0.8),
    "suspect_threshold": (0.05, 0.5),
    "deploy_confidence_base": (0.2, 0.6),
    "deploy_confidence_slope": (0.2, 0.8),
    "runbook_keyword_weight": (0.05, 0.4),
    "runbook_threshold": (0.1, 0.6),
    "dupe_overlap_weight": (0.1, 0.5),
    "dupe_same_service_weight": (0.0, 0.5),
    "dupe_threshold": (0.3, 0.9),
}


def load_weights(path: Path | None) -> Weights:
    if path is None or not path.exists():
        return Weights()
    data = yaml.safe_load(path.read_text()) or {}
    return Weights.model_validate(data)


def save_weights(w: Weights, path: Path) -> None:
    path.write_text(yaml.safe_dump(w.model_dump(), sort_keys=False))
