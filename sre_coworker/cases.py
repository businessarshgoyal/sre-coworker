"""Load and evaluate incident regression cases (tests/regression/cases/*.yaml).

Shared by the pytest suite and by `sre-coworker tune`, so both judge triage by the
exact same ground truth.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from sre_coworker.adapters import ADAPTERS
from sre_coworker.config import Settings
from sre_coworker.coworker import SRECoworker
from sre_coworker.models import Alert, Deploy, Incident, KnownIssue
from sre_coworker.weights import Weights


class StaticDeploys:
    def __init__(self, deploys: list[Deploy]) -> None:
        self.deploys = deploys

    async def recent(self, since: datetime) -> list[Deploy]:
        return [d for d in self.deploys if d.deployed_at >= since]


def load_cases(cases_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    return [(p, yaml.safe_load(p.read_text())) for p in sorted(cases_dir.glob("*.yaml"))]


def load_alert(case: dict[str, Any]) -> Alert:
    if "raw" in case:
        return ADAPTERS[case.get("source", "generic")](case["raw"])
    return Alert.model_validate(case["alert"])


async def run_case(
    case: dict[str, Any], workdir: Path, runbooks_dir: Path, weights: Weights | None = None
) -> Incident:
    known = [KnownIssue.model_validate(k) for k in case.get("known_issues", [])]
    issues_file = workdir / "known_issues.json"
    issues_file.write_text(json.dumps([k.model_dump(mode="json") for k in known]))
    settings = Settings(
        runbooks_dir=runbooks_dir,
        known_issues_file=issues_file,
        weights_file=workdir / "no-weights.yaml",
        cases_dir=workdir / "cases",
    )
    cw = SRECoworker(
        settings, StaticDeploys([Deploy.model_validate(d) for d in case.get("deploys", [])])
    )
    if weights is not None:
        cw.weights = weights
    inc = await cw.handle_alert(load_alert(case))
    return await cw.approve(inc.id, "regression")


def evaluate(inc: Incident, e: dict[str, Any]) -> list[str]:
    """Return the list of violated expectations (empty means the case passes)."""
    b = inc.brief
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    if "top_deploy" in e:
        top = b.suspect_deploys[0].deploy.sha if b.suspect_deploys else None
        check(top == e["top_deploy"], f"top_deploy={top!r} expected {e['top_deploy']!r}")
    for sha in e.get("not_suspect", []):
        check(all(d.deploy.sha != sha for d in b.suspect_deploys), f"{sha} should not be a suspect")
    if "is_duplicate" in e:
        check(b.is_duplicate == e["is_duplicate"], f"is_duplicate={b.is_duplicate}")
    if "duplicate_of" in e:
        key = b.known_issues[0].issue.key if b.known_issues else None
        check(key == e["duplicate_of"], f"duplicate_of={key!r} expected {e['duplicate_of']!r}")
    if "confidence_min" in e:
        check(
            b.confidence >= e["confidence_min"],
            f"confidence {b.confidence} < {e['confidence_min']}",
        )
    if "confidence_max" in e:
        check(
            b.confidence <= e["confidence_max"],
            f"confidence {b.confidence} > {e['confidence_max']}",
        )
    if "runbook" in e:
        slugs = [r.slug for r in b.runbooks]
        check(
            bool(slugs) and slugs[0] == e["runbook"],
            f"runbooks={slugs} expected {e['runbook']} first",
        )
    if "actions" in e:
        kinds = [a.kind for a in inc.actions]
        check(kinds == e["actions"], f"actions={kinds} expected {e['actions']}")
    if "fix_session" in e:
        sess = next((a for a in inc.actions if a.kind == "devin_session"), None)
        dispatched = sess is not None and sess.ref is not None
        check(dispatched == e["fix_session"], f"fix_session dispatched={dispatched}")
    for needle in e.get("action_contains", []):
        check(
            any(needle in a for a in b.recommended_actions),
            f"no recommended action contains {needle!r}",
        )
    if "severity" in e:
        check(inc.alert.severity.value == e["severity"], f"severity={inc.alert.severity.value}")
    if "service" in e:
        check(inc.alert.service == e["service"], f"service={inc.alert.service}")
    for label in e.get("citation_labels", []):
        check(any(c.label == label for c in b.citations), f"missing citation {label!r}")
    return failures


async def score_weights(
    weights: Weights,
    cases: list[tuple[Path, dict[str, Any]]],
    workdir: Path,
    runbooks_dir: Path,
) -> tuple[int, dict[str, list[str]]]:
    """(number of passing cases, {case stem: failures}) for a candidate weight set."""
    failing: dict[str, list[str]] = {}
    for path, case in cases:
        inc = await run_case(case, workdir, runbooks_dir, weights)
        f = evaluate(inc, case["expect"])
        if f:
            failing[path.stem] = f
    return len(cases) - len(failing), failing
