from __future__ import annotations

from pathlib import Path

from sre_coworker.config import Settings
from sre_coworker.connectors.deploys import DeploySource, FileDeploySource, GitHubDeploySource
from sre_coworker.connectors.devin import DevinClient
from sre_coworker.connectors.jira import JiraClient, load_known_issues
from sre_coworker.connectors.runbooks import Runbook, load_runbooks
from sre_coworker.models import ActionResult, Alert, Incident, IncidentState, KnownIssue
from sre_coworker.triage import triage


class SRECoworker:
    """Owns the loop: alert -> brief -> (approval) -> ticket + fix session."""

    def __init__(self, settings: Settings, deploy_source: DeploySource | None = None) -> None:
        self.settings = settings
        self.runbooks: list[Runbook] = load_runbooks(settings.runbooks_dir)
        self.incidents: dict[str, Incident] = {}
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
        inc.actions = await self._dispatch(inc)
        inc.state = IncidentState.actions_dispatched
        return inc

    def reject(self, incident_id: str, rejected_by: str) -> Incident:
        inc = self.incidents[incident_id]
        inc.state = IncidentState.rejected
        inc.approved_by = rejected_by
        return inc

    async def _dispatch(self, inc: Incident) -> list[ActionResult]:
        results: list[ActionResult] = []
        if inc.brief.is_duplicate:
            dup = inc.brief.known_issues[0].issue
            results.append(
                ActionResult(
                    kind="link_existing_ticket",
                    ok=True,
                    dry_run=True,
                    ref=dup.key,
                    url=dup.url,
                    detail="duplicate of open issue; no new ticket or fix session",
                )
            )
            return results

        ticket = await self._create_ticket(inc)
        results.append(ticket)
        if inc.brief.confidence < self.settings.min_confidence_for_fix_session:
            results.append(
                ActionResult(
                    kind="devin_session",
                    ok=True,
                    dry_run=True,
                    ref=None,
                    detail="skipped: confidence too low for an automated fix; human triage needed",
                )
            )
            return results
        results.append(await self._create_fix_session(inc, ticket.ref))
        return results

    async def _create_ticket(self, inc: Incident) -> ActionResult:
        if self.jira:
            return await self.jira.create(inc)
        return ActionResult(
            kind="jira_ticket",
            ok=True,
            dry_run=True,
            ref=f"{self.settings.jira_project_key}-DRYRUN",
            detail=f"[{inc.alert.service}] {inc.brief.headline}",
        )

    async def _create_fix_session(self, inc: Incident, ticket_ref: str | None) -> ActionResult:
        if self.devin:
            return await self.devin.create_fix_session(inc, ticket_ref)
        return ActionResult(
            kind="devin_session",
            ok=True,
            dry_run=True,
            ref="session-DRYRUN",
            detail=inc.brief.fix_prompt,
        )
