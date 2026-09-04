from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_FRONT_MATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.S)


@dataclass
class Runbook:
    slug: str
    title: str
    path: Path
    services: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    body: str = ""

    @property
    def remediation(self) -> list[str]:
        """Numbered/bulleted steps under a 'Remediation' heading."""
        steps: list[str] = []
        in_section = False
        for line in self.body.splitlines():
            if line.startswith("#"):
                in_section = "remediation" in line.lower()
                continue
            if in_section and re.match(r"^\s*(\d+\.|[-*])\s+", line):
                steps.append(re.sub(r"^\s*(\d+\.|[-*])\s+", "", line).strip())
        return steps


def load_runbooks(directory: Path) -> list[Runbook]:
    books: list[Runbook] = []
    for path in sorted(directory.glob("*.md")):
        text = path.read_text()
        meta: dict[str, Any] = {}
        body = text
        m = _FRONT_MATTER.match(text)
        if m:
            meta = yaml.safe_load(m.group(1)) or {}
            body = m.group(2)
        title = str(meta.get("title") or path.stem.replace("-", " ").title())
        books.append(
            Runbook(
                slug=path.stem,
                title=title,
                path=path,
                services=[str(s) for s in meta.get("services", [])],
                keywords=[str(k).lower() for k in meta.get("keywords", [])],
                body=body,
            )
        )
    return books
