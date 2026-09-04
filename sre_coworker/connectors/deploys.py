from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Protocol

import httpx

from sre_coworker.models import Deploy


class DeploySource(Protocol):
    async def recent(self, since: datetime) -> list[Deploy]: ...


class FileDeploySource:
    """Reads deploys from a JSON file (used for demos/tests)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def recent(self, since: datetime) -> list[Deploy]:
        raw = json.loads(self.path.read_text())
        deploys = [Deploy.model_validate(d) for d in raw]
        return [d for d in deploys if d.deployed_at >= since]


class GitHubDeploySource:
    """Treats commits on the default branch as deploys (good enough for trunk-based CD)."""

    def __init__(self, repo: str, token: str | None, branch: str = "main") -> None:
        self.repo = repo
        self.token = token
        self.branch = branch

    async def recent(self, since: datetime) -> list[Deploy]:
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"https://api.github.com/repos/{self.repo}/commits",
                params={"sha": self.branch, "since": since.isoformat(), "per_page": 30},
                headers=headers,
            )
            r.raise_for_status()
            commits = r.json()
            out: list[Deploy] = []
            for c in commits:
                files: list[str] = []
                detail = await client.get(c["url"], headers=headers)
                if detail.status_code == 200:
                    files = [f["filename"] for f in detail.json().get("files", [])]
                out.append(
                    Deploy(
                        sha=c["sha"][:10],
                        message=c["commit"]["message"].splitlines()[0],
                        author=c["commit"]["author"]["name"],
                        deployed_at=datetime.fromisoformat(
                            c["commit"]["committer"]["date"].replace("Z", "+00:00")
                        ),
                        url=c["html_url"],
                        files=files,
                    )
                )
            return out
