"""Active memory: procedures and facts the coworker learned from previous runs.

A *procedure* is a standing instruction bound to a guard the executor understands
(e.g. "search before creating a Jira issue"); a *fact* is knowledge injected into
prompts and plans (e.g. "project SRE has issue types Incident, Task"). Both carry the run
they were learned from so every behaviour change is traceable to evidence.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from sre_coworker.models import _now


class Guard(StrEnum):
    """Behaviours the executor can switch on. Learned procedures point at one of these."""

    search_before_create = "search_before_create"
    resolve_issue_type = "resolve_issue_type"
    retry_failed_step_only = "retry_failed_step_only"
    retry_with_backoff = "retry_with_backoff"
    none = "none"


class Memory(BaseModel):
    id: str = Field(default_factory=lambda: f"mem_{uuid4().hex[:8]}")
    kind: str = "procedure"  # procedure | fact
    text: str
    guard: Guard = Guard.none
    params: dict[str, Any] = Field(default_factory=dict)
    learned_from: str | None = None  # run id or "user"
    created_at: datetime = Field(default_factory=_now)
    times_applied: int = 0
    enabled: bool = True


class MemoryStore:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.items: list[Memory] = []
        if path and path.exists():
            self.items = [Memory.model_validate(m) for m in json.loads(path.read_text())]

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps([m.model_dump(mode="json") for m in self.items], indent=2)
            )

    def add(self, mem: Memory) -> Memory:
        """Idempotent: the same lesson (guard + params, or identical text) is one memory."""
        for m in self.items:
            same_rule = (mem.guard != Guard.none or mem.params) and (
                m.guard == mem.guard and m.params == mem.params
            )
            if same_rule or m.text == mem.text:
                return m
        self.items.append(mem)
        self.save()
        return mem

    def remove(self, mem_id: str) -> bool:
        before = len(self.items)
        self.items = [m for m in self.items if m.id != mem_id]
        self.save()
        return len(self.items) < before

    def active(self, guard: Guard, **match: Any) -> Memory | None:
        for m in self.items:
            if (
                m.enabled
                and m.guard == guard
                and all(m.params.get(k) == v for k, v in match.items())
            ):
                return m
        return None

    def facts(self) -> list[Memory]:
        return [m for m in self.items if m.enabled and m.kind == "fact"]

    def procedures(self) -> list[Memory]:
        return [m for m in self.items if m.enabled and m.kind == "procedure"]

    def applied(self, mem: Memory) -> None:
        mem.times_applied += 1
