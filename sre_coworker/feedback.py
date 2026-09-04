"""Turn a resolved incident into a regression case.

This is the learning loop: every human-confirmed outcome becomes ground truth that the
triage rules (and `sre-coworker tune`) are held to from then on.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from sre_coworker.models import Incident, Outcome


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def case_from_outcome(inc: Incident, outcome: Outcome) -> dict[str, Any]:
    alert = inc.alert.model_dump(mode="json", exclude_none=True)
    deploys = [d.model_dump(mode="json", exclude_none=True) for d in inc.brief.deploys_considered]
    issues = [
        k.model_dump(mode="json", exclude_none=True) for k in inc.brief.known_issues_considered
    ]

    expect: dict[str, Any] = {}
    if outcome.duplicate_of:
        expect["is_duplicate"] = True
        expect["duplicate_of"] = outcome.duplicate_of
    elif outcome.not_duplicate:
        expect["is_duplicate"] = False
    if outcome.culprit_sha:
        expect["top_deploy"] = outcome.culprit_sha
    if outcome.innocent_shas:
        expect["not_suspect"] = list(outcome.innocent_shas)
    if outcome.runbook:
        expect["runbook"] = outcome.runbook
    if outcome.needed_fix_session is not None:
        expect["fix_session"] = outcome.needed_fix_session
        if not outcome.needed_fix_session and not outcome.duplicate_of:
            expect["confidence_max"] = 0.5
        if outcome.needed_fix_session:
            expect["confidence_min"] = 0.5

    case: dict[str, Any] = {
        "name": f"[learned] {inc.alert.service}: {inc.alert.title}",
        "recorded_from": inc.id,
        "recorded_by": outcome.recorded_by,
        "alert": alert,
        "deploys": deploys,
        "expect": expect,
    }
    if issues:
        case["known_issues"] = issues
    if outcome.notes:
        case["notes"] = outcome.notes
    return case


def write_case(inc: Incident, outcome: Outcome, cases_dir: Path) -> Path:
    if not any(
        (outcome.culprit_sha, outcome.innocent_shas, outcome.duplicate_of, outcome.not_duplicate,
         outcome.runbook, outcome.needed_fix_session is not None)
    ):  # fmt: skip
        raise ValueError("outcome carries no assertable fact")
    cases_dir.mkdir(parents=True, exist_ok=True)
    stamp = inc.created_at.strftime("%Y%m%d%H%M%S")
    path = cases_dir / f"learned_{stamp}_{_slug(inc.alert.service)}_{inc.id[-6:]}.yaml"
    path.write_text(yaml.safe_dump(case_from_outcome(inc, outcome), sort_keys=False))
    return path
