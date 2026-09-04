"""Re-fit triage weights against the regression case set.

Hill-climb over the numeric fields in `weights.TUNABLE`: objective is the number of
passing cases, tie-broken by staying close to the current weights (so a tune run never
drifts the numbers without a case that justifies it). Deterministic for a given seed.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sre_coworker.cases import load_cases, score_weights
from sre_coworker.weights import TUNABLE, Weights


@dataclass
class TuneResult:
    before: int
    after: int
    total: int
    weights: Weights
    changed: dict[str, tuple[float, float]]
    still_failing: dict[str, list[str]]


def _distance(a: Weights, b: Weights) -> float:
    return math.sqrt(
        sum(((getattr(a, k) - getattr(b, k)) / (hi - lo)) ** 2 for k, (lo, hi) in TUNABLE.items())
    )


def _perturb(w: Weights, rng: random.Random, step: float) -> Weights:
    data: dict[str, Any] = w.model_dump()
    for k in rng.sample(sorted(TUNABLE), k=rng.randint(1, 3)):
        lo, hi = TUNABLE[k]
        data[k] = round(min(hi, max(lo, data[k] + rng.gauss(0.0, step * (hi - lo)))), 3)
    return Weights.model_validate(data)


async def tune(
    start: Weights,
    cases_dir: Path,
    runbooks_dir: Path,
    workdir: Path,
    iterations: int = 200,
    seed: int = 0,
) -> TuneResult:
    cases = load_cases(cases_dir)
    rng = random.Random(seed)
    best = start
    best_pass, best_fail = await score_weights(start, cases, workdir, runbooks_dir)
    before = best_pass
    step = 0.25
    for _ in range(iterations):
        if best_pass == len(cases):
            break
        cand = _perturb(best, rng, step)
        p, f = await score_weights(cand, cases, workdir, runbooks_dir)
        better = p > best_pass or (
            p == best_pass and _distance(cand, start) < _distance(best, start)
        )
        if better:
            best, best_pass, best_fail = cand, p, f
        step = max(0.03, step * 0.99)
    changed = {
        k: (getattr(start, k), getattr(best, k))
        for k in TUNABLE
        if getattr(start, k) != getattr(best, k)
    }
    return TuneResult(before, best_pass, len(cases), best, changed, best_fail)
