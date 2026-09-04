from __future__ import annotations

from pathlib import Path

from sre_coworker.config import Settings
from sre_coworker.connectors.deploys import DeploySource, FileDeploySource, GitHubDeploySource
from sre_coworker.connectors.devin import DevinClient
from sre_coworker.connectors.jira import JiraClient, load_known_issues
from sre_coworker.connectors.runbooks import Runbook, load_runbooks
from sre_coworker.executor import Executor
from sre_coworker.feedback import write_case
from sre_coworker.memory import Memory, MemoryStore
from sre_coworker.models import (
    Alert,
    Incident,
    IncidentState,
    KnownIssue,
    Outcome,
)
from sre_coworker.reflect import reflect_on_outcome, reflect_on_trace
from sre_coworker.tools import DryRunTools, LiveTools, Tools
from sre_coworker.trace import RunTrace
from sre_coworker.triage import triage
from sre_coworker.weights import Weights, load_weights


class SRECoworker:
    """Owns the loop: alert -> brief -> (approval) -> ticket + fix session."""

    def __init__(
        self,
        settings: Settings,
        deploy_source: DeploySource | None = None,
        tools: Tools | None = None,
    ) -> None:
        self.settings = settings
        self.runbooks: list[Runbook] = load_runbooks(settings.runbooks_dir)
        self.weights: Weights = load_weights(settings.weights_file)
        self.memory = MemoryStore(settings.memory_file)
        self.incidents: dict[str, Incident] = {}
        self.traces: dict[str, RunTrace] = {}
        self.last_learned: list[Memory] = []
        self.deploy_source: DeploySource = deploy_source or self._default_deploy_source()
        self.jira: JiraClient | None = None
        if settings.jira_live:
            assert settings.jira_base_url and settings.jira_email and settings.jira_api_token
            self.jira = JiraClient(
                settings.jira_base_url,
                settings.jira_email,
                settings.jira_api_token,
                settings.jira_project_key,
            )
        self.devin = (
            DevinClient(settings.devin_api_key, settings.devin_api_base)
            if settings.devin_live and settings.devin_api_key
            else None
        )
        live = self.jira is not None or self.devin is not None
        self.tools: Tools = tools or (
            LiveTools(self.jira, self.devin)
            if live
            else DryRunTools(
                project=settings.jira_project_key,
                seed_issues=load_known_issues(settings.known_issues_file),
            )
        )
        self.executor = Executor(
            self.tools,
            self.memory,
            settings.jira_project_key,
            settings.min_confidence_for_fix_session,
            dry_run=not live,
        )

    def _default_deploy_source(self) -> DeploySource:
        if self.settings.github_live and self.settings.github_repo:
            return GitHubDeploySource(self.settings.github_repo, self.settings.github_token)
        return FileDeploySource(Path("examples/deploys.json"))

    async def _known_issues(self, service: str) -> list[KnownIssue]:
        if self.jira:
            return await self.jira.search_open(service)
        return load_known_issues(self.settings.known_issues_file)

    async def handle_alert(self, alert: Alert) -> Incident:
        brief = await triage(
            alert,
            self.deploy_source,
            self.runbooks,
            await self._known_issues(alert.service),
            window_minutes=self.settings.deploy_window_minutes,
            repo=self.settings.github_repo,
            weights=self.weights,
        )
        incident = Incident(alert=alert, brief=brief, state=IncidentState.awaiting_approval)
        self.incidents[incident.id] = incident
        if brief.confidence >= self.settings.auto_approve_min_confidence:
            await self.approve(incident.id, approved_by="auto")
        return incident

    async def approve(self, incident_id: str, approved_by: str) -> Incident:
        inc = self.incidents[incident_id]
        if inc.state not in (IncidentState.awaiting_approval, IncidentState.triaged):
            raise ValueError(f"incident {incident_id} is {inc.state}, cannot approve")
        inc.state = IncidentState.approved
        inc.approved_by = approved_by
        inc.actions, trace = await self.executor.run(inc)
        self.traces[inc.id] = trace
        inc.run_id = trace.run_id
        self.last_learned = reflect_on_trace(trace, self.memory, self.settings.jira_project_key)
        inc.state = IncidentState.actions_dispatched
        return inc

    def reject(self, incident_id: str, rejected_by: str) -> Incident:
        inc = self.incidents[incident_id]
        inc.state = IncidentState.rejected
        inc.approved_by = rejected_by
        return inc

    def record_outcome(self, incident_id: str, outcome: Outcome) -> Path:
        """Persist ground truth for a resolved incident as a regression case."""
        inc = self.incidents[incident_id]
        inc.outcome = outcome
        self.last_learned = reflect_on_outcome(
            inc, outcome, self.traces.get(incident_id), self.memory
        )
        return write_case(inc, outcome, self.settings.cases_dir)
