"""Tool surface the executor calls (`jira.*`, `devin.*`).

`DryRunTools` is an in-memory Jira/Devin with the failure modes real ones have (invalid
issue type per project, duplicate tickets accumulating across runs, transient rate
limits) so the learning loop can be exercised without credentials. `LiveTools` maps the
same surface onto the HTTP connectors.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field

from sre_coworker.connectors.devin import DevinClient
from sre_coworker.connectors.jira import JiraClient, render_brief
from sre_coworker.models import Incident, KnownIssue
from sre_coworker.trace import ToolError
from sre_coworker.triage import tokens


class IssueRef(BaseModel):
    key: str
    url: str | None = None
    summary: str = ""
    status: str = "Open"


class SessionRef(BaseModel):
    session_id: str
    url: str | None = None


class Tools(Protocol):
    async def jira_search_issues(
        self, project: str, service: str, query: str, error_signature: str | None
    ) -> list[IssueRef]: ...
    async def jira_get_issue_types(self, project: str) -> list[str]: ...
    async def jira_create_issue(
        self, project: str, issue_type: str, summary: str, description: str, labels: list[str]
    ) -> IssueRef: ...
    async def devin_create_session(
        self, prompt: str, title: str, tags: list[str]
    ) -> SessionRef: ...


class _StoredIssue(BaseModel):
    key: str
    summary: str
    status: str = "Open"
    service: str | None = None
    error_signature: str | None = None
    labels: list[str] = Field(default_factory=list)


class DryRunTools:
    def __init__(
        self,
        project: str = "ENG",
        issue_types: dict[str, list[str]] | None = None,
        devin_transient_failures: int = 0,
        seed_issues: list[KnownIssue] | None = None,
    ) -> None:
        self.issue_types = issue_types or {project: ["Incident", "Task"]}
        self.devin_transient_failures = devin_transient_failures
        self.issues: list[_StoredIssue] = [
            _StoredIssue(
                key=k.key,
                summary=k.summary,
                status=k.status,
                service=k.service,
                error_signature=k.error_signature,
            )
            for k in seed_issues or []
        ]
        self.sessions: list[SessionRef] = []
        self._seq = len(self.issues)

    async def jira_search_issues(
        self, project: str, service: str, query: str, error_signature: str | None
    ) -> list[IssueRef]:
        q = tokens(query)
        out: list[IssueRef] = []
        for it in self.issues:
            if it.status.lower() in {"done", "closed", "resolved"}:
                continue
            if not it.key.startswith(project + "-"):
                continue
            sig_hit = bool(error_signature) and it.error_signature == error_signature
            svc_hit = it.service == service and len(q & tokens(it.summary)) >= 2
            if sig_hit or svc_hit:
                out.append(IssueRef(key=it.key, summary=it.summary, status=it.status))
        return out

    async def jira_get_issue_types(self, project: str) -> list[str]:
        if project not in self.issue_types:
            raise ToolError("PROJECT_NOT_FOUND", f"no project {project}")
        return list(self.issue_types[project])

    async def jira_create_issue(
        self, project: str, issue_type: str, summary: str, description: str, labels: list[str]
    ) -> IssueRef:
        valid = await self.jira_get_issue_types(project)
        if issue_type not in valid:
            raise ToolError(
                "INVALID_ISSUE_TYPE",
                f"issue type '{issue_type}' is not valid for project {project}; "
                f"valid: {', '.join(valid)}",
            )
        self._seq += 1
        key = f"{project}-{self._seq}"
        service = next((lab.removeprefix("svc:") for lab in labels if lab.startswith("svc:")), None)
        sig = next((lab.removeprefix("sig:") for lab in labels if lab.startswith("sig:")), None)
        self.issues.append(
            _StoredIssue(
                key=key, summary=summary, service=service, error_signature=sig, labels=labels
            )
        )
        return IssueRef(key=key, summary=summary)

    async def devin_create_session(self, prompt: str, title: str, tags: list[str]) -> SessionRef:
        if self.devin_transient_failures > 0:
            self.devin_transient_failures -= 1
            raise ToolError("RATE_LIMITED", "429 from api.devin.ai; retry later", retryable=True)
        ref = SessionRef(session_id=f"session-{len(self.sessions) + 1:04d}")
        self.sessions.append(ref)
        return ref


class LiveTools:
    def __init__(self, jira: JiraClient | None, devin: DevinClient | None) -> None:
        self.jira = jira
        self.devin = devin

    def _need_jira(self) -> JiraClient:
        if self.jira is None:
            raise ToolError("NOT_CONFIGURED", "Jira is not configured")
        return self.jira

    async def jira_search_issues(
        self, project: str, service: str, query: str, error_signature: str | None
    ) -> list[IssueRef]:
        j = self._need_jira()
        q = tokens(query)
        out: list[IssueRef] = []
        for k in await j.search_open(service):
            if (error_signature and k.error_signature == error_signature) or (
                k.service == service and len(q & tokens(k.summary)) >= 2
            ):
                out.append(IssueRef(key=k.key, url=k.url, summary=k.summary, status=k.status))
        return out

    async def jira_get_issue_types(self, project: str) -> list[str]:
        j = self._need_jira()
        async with httpx.AsyncClient(timeout=20, auth=j.auth) as client:
            r = await client.get(f"{j.base_url}/rest/api/3/project/{project}")
            if r.status_code == 404:
                raise ToolError("PROJECT_NOT_FOUND", f"no project {project}")
            r.raise_for_status()
            return [t["name"] for t in r.json().get("issueTypes", [])]

    async def jira_create_issue(
        self, project: str, issue_type: str, summary: str, description: str, labels: list[str]
    ) -> IssueRef:
        j = self._need_jira()
        payload: dict[str, Any] = {
            "fields": {
                "project": {"key": project},
                "summary": summary,
                "issuetype": {"name": issue_type},
                "labels": labels,
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": description}]}
                    ],
                },
            }
        }
        async with httpx.AsyncClient(timeout=20, auth=j.auth) as client:
            r = await client.post(f"{j.base_url}/rest/api/3/issue", json=payload)
            if r.status_code == 400 and "issuetype" in r.text:
                raise ToolError("INVALID_ISSUE_TYPE", r.text[:200])
            r.raise_for_status()
            key = r.json()["key"]
            return IssueRef(key=key, url=f"{j.base_url}/browse/{key}", summary=summary)

    async def devin_create_session(self, prompt: str, title: str, tags: list[str]) -> SessionRef:
        if self.devin is None:
            raise ToolError("NOT_CONFIGURED", "Devin is not configured")
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self.devin.base_url}/sessions",
                json={"prompt": prompt, "title": title, "tags": tags, "idempotent": True},
                headers={"Authorization": f"Bearer {self.devin.api_key}"},
            )
            if r.status_code == 429:
                raise ToolError("RATE_LIMITED", "429 from api.devin.ai", retryable=True)
            r.raise_for_status()
            data = r.json()
            return SessionRef(session_id=data["session_id"], url=data.get("url"))


def describe_incident(inc: Incident) -> str:
    return render_brief(inc.brief)
