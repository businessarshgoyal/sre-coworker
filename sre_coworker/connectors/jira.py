from __future__ import annotations

import json
from pathlib import Path

import httpx

from sre_coworker.models import ActionResult, Brief, Incident, KnownIssue


class JiraClient:
    def __init__(self, base_url: str, email: str, token: str, project_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = (email, token)
        self.project_key = project_key

    async def search_open(self, service: str | None) -> list[KnownIssue]:
        jql = f"project = {self.project_key} AND statusCategory != Done ORDER BY created DESC"
        async with httpx.AsyncClient(timeout=20, auth=self.auth) as client:
            r = await client.get(
                f"{self.base_url}/rest/api/3/search",
                params={"jql": jql, "maxResults": 50, "fields": "summary,status,labels"},
            )
            r.raise_for_status()
            issues = []
            for it in r.json().get("issues", []):
                f = it["fields"]
                issues.append(
                    KnownIssue(
                        key=it["key"],
                        summary=f["summary"],
                        status=f["status"]["name"],
                        url=f"{self.base_url}/browse/{it['key']}",
                        service=next(
                            (
                                lab.removeprefix("svc:")
                                for lab in f.get("labels", [])
                                if lab.startswith("svc:")
                            ),
                            None,
                        ),
                    )
                )
            return issues

    async def create(self, incident: Incident) -> ActionResult:
        b = incident.brief
        payload = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": f"[{incident.alert.service}] {b.headline}",
                "issuetype": {"name": "Bug"},
                "labels": ["sre-coworker", f"svc:{incident.alert.service}"],
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": render_brief(b)}],
                        }
                    ],
                },
            }
        }
        async with httpx.AsyncClient(timeout=20, auth=self.auth) as client:
            r = await client.post(f"{self.base_url}/rest/api/3/issue", json=payload)
            r.raise_for_status()
            key = r.json()["key"]
            return ActionResult(
                kind="jira_ticket",
                ok=True,
                dry_run=False,
                ref=key,
                url=f"{self.base_url}/browse/{key}",
            )


def load_known_issues(path: Path) -> list[KnownIssue]:
    if not path.exists():
        return []
    return [KnownIssue.model_validate(i) for i in json.loads(path.read_text())]


def render_brief(b: Brief) -> str:
    lines = [b.summary, "", f"Likely cause: {b.likely_cause} (confidence {b.confidence:.0%})", ""]
    if b.suspect_deploys:
        lines.append("Suspect deploys:")
        lines += [
            f"  - {d.deploy.sha} {d.deploy.message} ({d.minutes_before_alert:.0f} min before alert)"
            for d in b.suspect_deploys
        ]
    if b.runbooks:
        lines.append("Runbooks: " + ", ".join(r.title for r in b.runbooks))
    if b.recommended_actions:
        lines.append("Recommended actions:")
        lines += [f"  {i}. {a}" for i, a in enumerate(b.recommended_actions, 1)]
    if b.citations:
        lines.append("Sources:")
        lines += [f"  - {c.label}: {c.url or c.excerpt or ''}" for c in b.citations]
    return "\n".join(lines)
