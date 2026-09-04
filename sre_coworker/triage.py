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

_STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "for",
    "is",
    "are",
    "with",
    "after",
    "before",
    "from",
    "by",
    "at",
    "be",
    "this",
    "that",
    "it",
    "as",
    "error",
    "errors",
    "failing",
    "failed",
    "failure",
    "spike",
    "high",
    "rate",
    "alert",
}


def tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z][a-z0-9_\-]{2,}", text.lower())
    return {w for w in words if w not in _STOP}


def correlate_deploys(alert: Alert, deploys: list[Deploy]) -> list[DeployCorrelation]:
    alert_terms = tokens(f"{alert.title} {alert.description} {alert.service}")
    out: list[DeployCorrelation] = []
    for d in deploys:
        if d.deployed_at > alert.fired_at:
            continue
        minutes = (alert.fired_at - d.deployed_at) / timedelta(minutes=1)
        deploy_terms = tokens(d.message + " " + " ".join(d.files))
        hits = sorted(alert_terms & deploy_terms)
        # recency dominates; each keyword hit adds weight
        recency = max(0.0, 1.0 - minutes / 240.0)
        score = 0.6 * recency + 0.4 * min(1.0, len(hits) / 3)
        service_match = alert.service.lower() in deploy_terms or any(
            alert.service.split("-")[0] in f for f in d.files
        )
        if service_match:
            score += 0.15
        elif not hits:
            score *= 0.5
        if score > 0.2:
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


def match_runbooks(alert: Alert, runbooks: list[Runbook]) -> list[RunbookMatch]:
    alert_terms = tokens(f"{alert.title} {alert.description} {alert.error_signature or ''}")
    out: list[RunbookMatch] = []
    for rb in runbooks:
        score = 0.0
        if alert.service in rb.services:
            score += 0.5
        kw_hits = [k for k in rb.keywords if k in alert_terms or k in alert.title.lower()]
        score += min(0.5, 0.2 * len(kw_hits))
        if score >= 0.3:
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


def match_known_issues(alert: Alert, issues: list[KnownIssue]) -> list[KnownIssueMatch]:
    out: list[KnownIssueMatch] = []
    alert_terms = tokens(alert.title + " " + alert.description)
    for issue in issues:
        if issue.status.lower() in {"done", "closed", "resolved"}:
            continue
        if alert.error_signature and issue.error_signature == alert.error_signature:
            out.append(KnownIssueMatch(issue=issue, reason="identical error signature", score=1.0))
            continue
        overlap = alert_terms & tokens(issue.summary)
        same_service = issue.service == alert.service
        score = min(1.0, 0.25 * len(overlap)) + (0.3 if same_service else 0.0)
        if score >= 0.55:
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
) -> Brief:
    deploys = await deploy_source.recent(alert.fired_at - timedelta(minutes=window_minutes))
    corr = correlate_deploys(alert, deploys)
    rbs = match_runbooks(alert, runbooks)
    dupes = match_known_issues(alert, known_issues)

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
        confidence = 0.9
        actions.append(f"Link alert to {k} instead of opening a new ticket")
        actions.append("Ping the owner of the existing ticket; do not page on-call")
    elif corr:
        top = corr[0]
        likely = (
            f'Regression from deploy {top.deploy.sha} ("{top.deploy.message}"), '
            f"{top.minutes_before_alert:.0f} min before alert"
            + (f"; keyword overlap: {', '.join(top.keyword_hits)}" if top.keyword_hits else "")
        )
        related = bool(top.keyword_hits) or top.service_match
        confidence = min(0.95, 0.4 + top.score * 0.5) if related else 0.4
        actions.append(
            f"Prepare rollback of {top.deploy.sha}; execute if error rate does not recover"
        )
        actions.append("Open fix ticket and dispatch a Devin session to draft the fix PR")
    else:
        likely = (
            "No recent deploy or known issue matches; likely infrastructure or upstream dependency"
        )
        confidence = 0.3
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
    )
