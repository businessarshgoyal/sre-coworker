from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(UTC)


class Severity(StrEnum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


class Alert(BaseModel):
    """Normalised alert. Adapters map Datadog/Sentry/Grafana payloads into this."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    source: str = "generic"
    service: str
    title: str
    description: str = ""
    severity: Severity = Severity.high
    error_signature: str | None = None
    fired_at: datetime = Field(default_factory=_now)
    tags: dict[str, str] = Field(default_factory=dict)
    url: str | None = None


class Deploy(BaseModel):
    sha: str
    message: str
    author: str
    deployed_at: datetime
    url: str | None = None
    files: list[str] = Field(default_factory=list)


class DeployCorrelation(BaseModel):
    deploy: Deploy
    minutes_before_alert: float
    keyword_hits: list[str] = Field(default_factory=list)
    service_match: bool = False
    score: float


class RunbookMatch(BaseModel):
    slug: str
    title: str
    path: str
    score: float
    remediation: list[str] = Field(default_factory=list)


class KnownIssue(BaseModel):
    key: str
    summary: str
    status: str
    url: str | None = None
    error_signature: str | None = None
    service: str | None = None


class KnownIssueMatch(BaseModel):
    issue: KnownIssue
    reason: str
    score: float


class Citation(BaseModel):
    label: str
    url: str | None = None
    excerpt: str | None = None


class Brief(BaseModel):
    """The root-cause brief handed to a human for approval."""

    headline: str
    summary: str
    likely_cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    suspect_deploys: list[DeployCorrelation] = Field(default_factory=list)
    runbooks: list[RunbookMatch] = Field(default_factory=list)
    known_issues: list[KnownIssueMatch] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    fix_prompt: str
    citations: list[Citation] = Field(default_factory=list)
    is_duplicate: bool = False
    # raw inputs, retained so an outcome can be replayed as a regression case
    deploys_considered: list[Deploy] = Field(default_factory=list, exclude=True)
    known_issues_considered: list[KnownIssue] = Field(default_factory=list, exclude=True)


class IncidentState(StrEnum):
    triaged = "triaged"
    awaiting_approval = "awaiting_approval"
    approved = "approved"
    rejected = "rejected"
    actions_dispatched = "actions_dispatched"


class ActionResult(BaseModel):
    kind: str
    ok: bool
    dry_run: bool
    ref: str | None = None
    url: str | None = None
    detail: str | None = None


class Outcome(BaseModel):
    """What actually happened, recorded by a human after the incident is resolved."""

    culprit_sha: str | None = None
    innocent_shas: list[str] = Field(default_factory=list)
    duplicate_of: str | None = None
    not_duplicate: bool = False
    runbook: str | None = None
    needed_fix_session: bool | None = None
    notes: str = ""
    recorded_by: str = "unknown"


class Incident(BaseModel):
    id: str = Field(default_factory=lambda: f"inc_{uuid4().hex[:10]}")
    alert: Alert
    brief: Brief
    state: IncidentState = IncidentState.triaged
    created_at: datetime = Field(default_factory=_now)
    approved_by: str | None = None
    actions: list[ActionResult] = Field(default_factory=list)
    outcome: Outcome | None = None
