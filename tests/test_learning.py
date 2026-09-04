"""The self-improvement loop: outcome -> regression case -> tune -> weights that pass it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from sre_coworker.cases import StaticDeploys, evaluate, load_cases, run_case, score_weights
from sre_coworker.config import Settings
from sre_coworker.coworker import SRECoworker
from sre_coworker.models import Alert, Deploy, Outcome
from sre_coworker.tune import tune
from sre_coworker.weights import load_weights, save_weights

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _deploys() -> list[Deploy]:
    return [
        Deploy(
            sha="culprit00",
            message="orders: rewrite pagination cursor encoding",
            author="a",
            deployed_at=NOW - timedelta(minutes=150),
            files=["orders_api/pagination.py"],
        ),
        Deploy(
            sha="innocent0",
            message="billing: bump invoice template",
            author="b",
            deployed_at=NOW - timedelta(minutes=5),
            files=["billing/templates/invoice.html"],
        ),
    ]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        runbooks_dir=ROOT / "runbooks",
        known_issues_file=tmp_path / "none.json",
        weights_file=tmp_path / "weights.yaml",
        cases_dir=tmp_path / "cases",
    )


async def test_outcome_becomes_regression_case(tmp_path: Path) -> None:
    cw = SRECoworker(_settings(tmp_path), StaticDeploys(_deploys()))
    alert = Alert(
        service="orders-api",
        title="orders-api 5xx spike on list endpoint",
        description="cursor decode errors",
        fired_at=NOW,
    )
    inc = await cw.handle_alert(alert)
    outcome = Outcome(culprit_sha="culprit00", innocent_shas=["innocent0"], recorded_by="oncall")
    path = cw.record_outcome(inc.id, outcome)

    case = yaml.safe_load(path.read_text())
    assert case["expect"] == {"top_deploy": "culprit00", "not_suspect": ["innocent0"]}
    assert [d["sha"] for d in case["deploys"]] == ["culprit00", "innocent0"]
    assert case["recorded_from"] == inc.id
    assert inc.outcome == outcome

    # current weights get this wrong (recent unrelated deploy still listed as a suspect) ...
    replay = await run_case(case, tmp_path, ROOT / "runbooks", cw.weights)
    assert evaluate(replay, case["expect"]) == ["innocent0 should not be a suspect"]

    # ... so tuning against the recorded case must fix it, and the fix must persist
    res = await tune(cw.weights, cw.settings.cases_dir, ROOT / "runbooks", tmp_path, seed=0)
    assert (res.before, res.after, res.total) == (0, 1, 1)
    save_weights(res.weights, cw.settings.weights_file)
    cw2 = SRECoworker(cw.settings, StaticDeploys(_deploys()))
    assert cw2.weights == res.weights
    inc2 = await cw2.handle_alert(alert)
    assert [d.deploy.sha for d in inc2.brief.suspect_deploys] == ["culprit00"]


async def test_outcome_without_facts_is_rejected(tmp_path: Path) -> None:
    cw = SRECoworker(_settings(tmp_path), StaticDeploys(_deploys()))
    inc = await cw.handle_alert(Alert(service="orders-api", title="x", fired_at=NOW))
    try:
        cw.record_outcome(inc.id, Outcome(notes="nothing to assert"))
    except ValueError:
        return
    raise AssertionError("expected ValueError")


async def test_tune_recovers_from_bad_weights(tmp_path: Path) -> None:
    """Start from weights that fail cases; tune must find weights that pass all of them."""
    cases_dir = ROOT / "tests" / "regression" / "cases"
    good = load_weights(ROOT / "weights.yaml")
    bad = good.model_copy(
        update={"recency_weight": 0.9, "keyword_weight": 0.1, "service_bonus": 0.0}
    )
    cases = load_cases(cases_dir)
    bad_pass, _ = await score_weights(bad, cases, tmp_path, ROOT / "runbooks")
    assert bad_pass < len(cases), "bad weights should fail at least one case"

    res = await tune(bad, cases_dir, ROOT / "runbooks", tmp_path, iterations=150, seed=1)
    assert res.before == bad_pass
    assert res.after > res.before
    assert res.changed

    # round-trip through the weights file the coworker actually loads
    save_weights(res.weights, tmp_path / "weights.yaml")
    assert load_weights(tmp_path / "weights.yaml") == res.weights


async def test_tune_is_a_noop_when_all_cases_pass(tmp_path: Path) -> None:
    shipped = load_weights(ROOT / "weights.yaml")
    res = await tune(
        shipped, ROOT / "tests" / "regression" / "cases", ROOT / "runbooks", tmp_path, seed=0
    )
    assert res.before == res.after == res.total
    assert res.changed == {}
