"""Scenario regression suite: one YAML per incident shape under tests/regression/cases.

Each case fixes the alert, deploys and known issues, then asserts on the brief and the
actions the coworker dispatches. Cases are added by hand when triage gets an incident wrong,
and automatically by `record_outcome` after a real incident is resolved.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sre_coworker.cases import evaluate, run_case
from sre_coworker.weights import load_weights

ROOT = Path(__file__).resolve().parents[2]
CASES = sorted((Path(__file__).parent / "cases").glob("*.yaml"))
WEIGHTS = load_weights(ROOT / "weights.yaml")


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
async def test_case(path: Path, tmp_path: Path) -> None:
    case = yaml.safe_load(path.read_text())
    inc = await run_case(case, tmp_path, ROOT / "runbooks", WEIGHTS)
    failures = evaluate(inc, case["expect"])
    assert not failures, f"{case['name']}:\n  - " + "\n  - ".join(failures)
