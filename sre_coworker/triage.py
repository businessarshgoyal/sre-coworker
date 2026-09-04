"""Deterministic triage: correlate deploys, match runbooks, dedupe known issues, write a brief.

No LLM required; every claim in the brief has a citation to a deploy, runbook or ticket.
"""

from __future__ import annotations

import re
from datetime import timedelta

from sre_coworker.connectors.deploys import DeploySource
from sre_coworker.connectors.runbooks import Runbook
from sre_coworker.models import (
    Alert,
    Brief,
    Citation,
    Deploy,
    DeployCorrelation,
    KnownIssue,
    KnownIssueMatch,
    RunbookMatch,
)
from sre_coworker.weights import Weights

DEFAULT_WEIGHTS = Weights()

_NON_CODE = re.compile(r"(^|/)(readme|changelog|license|contributing)|\.(md|rst|txt|adoc)$", re.I)
_REVERT = re.compile(r"^\s*revert\b", re.I)


def is_docs_only(deploy: Deploy) -> bool:
    return bool(deploy.files) and all(_NON_CODE.search(f) for f in deploy.files)


def is_revert(deploy: Deploy) -> bool:
    return bool(_REVERT.match(deploy.message))


def tokens(text: str, w: Weights = DEFAULT_WEIGHTS) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9_\-]{1,}", text.lower())
    stop, canon = w.stop_set(), w.canonical()
    return {canon.get(x, x) for x in words if x not in stop and len(x) >= 3}


def correlate_deploys(
    alert: Alert, deploys: list[Deploy], w: Weights = DEFAULT_WEIGHTS
) -> list[DeployCorrelation]:
    alert_terms = tokens(f"{alert.title} {alert.description} {alert.service}", w)
    out: list[DeployCorrelation] = []
    for d in deploys:
        if d.deployed_at > alert.fired_at or is_docs_only(d):
            continue
        minutes = (alert.fired_at - d.deployed_at) / timedelta(minutes=1)
        deploy_terms = tokens(d.message + " " + " ".join(d.files), w)
        hits = sorted(alert_terms & deploy_terms)
        recency = max(0.0, 1.0 - minutes / 240.0)
        score = w.recency_weight * recency + w.keyword_weight * min(
            1.0, len(hits) / w.keyword_saturation
        )
        service_match = alert.service.lower() in deploy_terms or any(
            alert.service.split("-")[0] in f for f in d.files
        )
        if service_match:
            score += w.service_bonus
        elif not hits:
            score *= w.unrelated_penalty
        if is_revert(d):
            score *= w.revert_penalty
        if score > w.suspect_threshold:
            out.append(
                DeployCorrelation(
                    deploy=d,
                    minutes_before_alert=minutes,
                    keyword_hits=hits,
                    service_match=service_match,
                    score=round(score, 3),
                )
            )
    return sorted(out, key=lambda c: c.score, reverse=True)[:3]


def match_runbooks(
    alert: Alert, runbooks: list[Runbook], w: Weights = DEFAULT_WEIGHTS
) -> list[RunbookMatch]:
    alert_terms = tokens(f"{alert.title} {alert.description} {alert.error_signature or ''}", w)
    canon = w.canonical()
    out: list[RunbookMatch] = []
    for rb in runbooks:
        score = 0.0
        if alert.service in rb.services:
            score += w.runbook_service_weight
        kw_hits = [
            k for k in rb.keywords if canon.get(k, k) in alert_terms or k in alert.title.lower()
        ]
        score += min(0.5, w.runbook_keyword_weight * len(kw_hits))
        if score >= w.runbook_threshold:
            out.append(
                RunbookMatch(
                    slug=rb.slug,
                    title=rb.title,
                    path=str(rb.path),
                    score=round(score, 3),
                    remediation=rb.remediation,
                )
            )
    return sorted(out, key=lambda m: m.score, reverse=True)[:2]


def match_known_issues(
    alert: Alert, issues: list[KnownIssue], w: Weights = DEFAULT_WEIGHTS
) -> list[KnownIssueMatch]:
    out: list[KnownIssueMatch] = []
    alert_terms = tokens(alert.title + " " + alert.description, w)
    for issue in issues:
        if issue.status.lower() in {"done", "closed", "resolved"}:
            continue
        if alert.error_signature and issue.error_signature == alert.error_signature:
            out.append(KnownIssueMatch(issue=issue, reason="identical error signature", score=1.0))
            continue
        overlap = alert_terms & tokens(issue.summary, w)
        same_service = issue.service == alert.service
        score = min(1.0, w.dupe_overlap_weight * len(overlap)) + (
            w.dupe_same_service_weight if same_service else 0.0
        )
        if score >= w.dupe_threshold:
            out.append(
                KnownIssueMatch(
                    issue=issue,
                    reason=f"summary overlap: {', '.join(sorted(overlap))}",
                    score=round(score, 3),
                )
            )
    return sorted(out, key=lambda m: m.score, reverse=True)[:3]


def _fix_prompt(
    alert: Alert, deploys: list[DeployCorrelation], runbooks: list[RunbookMatch], repo: str | None
) -> str:
    lines = [
        f"Production incident on `{alert.service}`: {alert.title}.",
        alert.description,
        "",
    ]
    if repo:
        lines.append(f"Repository: {repo}.")
    if deploys:
        top = deploys[0]
        lines.append(
            f'Most likely trigger: commit {top.deploy.sha} ("{top.deploy.message}") '
            f"deployed {top.minutes_before_alert:.0f} minutes before the alert."
        )
        if top.deploy.files:
            lines.append("Files touched: " + ", ".join(top.deploy.files[:10]) + ".")
    if runbooks and runbooks[0].remediation:
        lines.append("Runbook remediation steps:")
        lines += [f"- {s}" for s in runbooks[0].remediation]
    lines += [
        "",
        "Task: reproduce with a failing test, implement the smallest safe fix (or a revert if the "
        "commit above is clearly at fault), run the test suite, and open a PR. In the PR body, "
        "link this incident and explain root cause. Do not merge.",
    ]
    return "\n".join(lines).replace("\n\n\n", "\n\n")


async def triage(
    alert: Alert,
    deploy_source: DeploySource,
    runbooks: list[Runbook],
    known_issues: list[KnownIssue],
    window_minutes: int = 240,
    repo: str | None = None,
    weights: Weights = DEFAULT_WEIGHTS,
) -> Brief:
    w = weights
    deploys = await deploy_source.recent(alert.fired_at - timedelta(minutes=window_minutes))
    corr = correlate_deploys(alert, deploys, w)
    rbs = match_runbooks(alert, runbooks, w)
    dupes = match_known_issues(alert, known_issues, w)

    citations: list[Citation] = []
    if alert.url:
        citations.append(Citation(label=f"{alert.source} alert", url=alert.url))
    citations += [
        Citation(label=f"deploy {c.deploy.sha}", url=c.deploy.url, excerpt=c.deploy.message)
        for c in corr
    ]
    citations += [Citation(label=f"runbook: {r.title}", excerpt=r.path) for r in rbs]
    citations += [
        Citation(label=f"ticket {d.issue.key}", url=d.issue.url, excerpt=d.issue.summary)
        for d in dupes
    ]

    is_dup = bool(dupes) and dupes[0].score >= 0.9
    actions: list[str] = []
    if is_dup:
        k = dupes[0].issue.key
        likely = f"Recurrence of known issue {k}: {dupes[0].issue.summary}"
        confidence = w.dupe_confidence
        actions.append(f"Link alert to {k} instead of opening a new ticket")
        actions.append("Ping the owner of the existing ticket; do not page on-call")
    elif corr and is_revert(corr[0].deploy):
        top = corr[0]
        likely = (
            f'Only recent change is a revert ({top.deploy.sha}, "{top.deploy.message}"); '
            "the incident may predate it or the revert may be incomplete"
        )
        confidence = w.revert_confidence
        actions.append("Confirm whether the revert fully rolled back; do not auto-dispatch a fix")
        actions.append("Page on-call: incident persists after a rollback attempt")
    elif corr:
        top = corr[0]
        likely = (
            f'Regression from deploy {top.deploy.sha} ("{top.deploy.message}"), '
            f"{top.minutes_before_alert:.0f} min before alert"
            + (f"; keyword overlap: {', '.join(top.keyword_hits)}" if top.keyword_hits else "")
        )
        related = bool(top.keyword_hits) or top.service_match
        confidence = (
            min(0.95, w.deploy_confidence_base + top.score * w.deploy_confidence_slope)
            if related
            else w.unrelated_confidence
        )
        actions.append(
            f"Prepare rollback of {top.deploy.sha}; execute if error rate does not recover"
        )
        actions.append("Open fix ticket and dispatch a Devin session to draft the fix PR")
    else:
        likely = (
            "No recent deploy or known issue matches; likely infrastructure or upstream dependency"
        )
        confidence = w.no_match_confidence
        actions.append("Page on-call: no confident automated diagnosis")
    for rb in rbs[:1]:
        actions += [f"Runbook '{rb.title}': {s}" for s in rb.remediation[:3]]

    summary = (
        f"{alert.severity.value.upper()} alert on {alert.service} from {alert.source} at "
        f"{alert.fired_at:%H:%M UTC}. {len(deploys)} deploy(s) in the last {window_minutes} min, "
        f"{len(corr)} suspect; {len(rbs)} runbook(s) matched; {len(dupes)} open ticket(s) similar."
    )
    return Brief(
        headline=alert.title,
        summary=summary,
        likely_cause=likely,
        confidence=round(confidence, 2),
        suspect_deploys=corr,
        runbooks=rbs,
        known_issues=dupes,
        recommended_actions=actions,
        fix_prompt=_fix_prompt(alert, corr, rbs, repo),
        citations=citations,
        is_duplicate=is_dup,
        deploys_considered=deploys,
        known_issues_considered=known_issues,
    )
