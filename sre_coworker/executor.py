"""Executes an approved incident's actions as a sequence of tool calls, guided by memory.

Without memory the executor behaves like a naive first run: create a Jira issue with the
default type, dispatch Devin, and on any failure rerun the whole batch. Each learned
procedure (see memory.Guard) switches on a smarter behaviour; every call is traced so
reflect.py can learn from what happened.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from sre_coworker.memory import Guard, Memory, MemoryStore
from sre_coworker.models import ActionResult, Incident
from sre_coworker.tools import IssueRef, SessionRef, Tools, describe_incident
from sre_coworker.trace import Recorder, RunTrace, ToolError

DEFAULT_ISSUE_TYPE = "Bug"
PREFERRED_ISSUE_TYPES = ("Incident", "Bug", "Task")
MAX_BATCH_RESTARTS = 1


class Executor:
    def __init__(
        self,
        tools: Tools,
        memory: MemoryStore,
        project: str,
        min_confidence_for_fix_session: float,
        dry_run: bool = True,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.tools = tools
        self.dry_run = dry_run
        self.memory = memory
        self.project = project
        self.min_conf = min_confidence_for_fix_session
        self.sleep = sleep

    def _use(self, rec: Recorder, guard: Guard, **match: object) -> Memory | None:
        m = self.memory.active(guard, **match)
        if m is not None and m.id not in rec.trace.memories_applied:
            self.memory.applied(m)
            rec.trace.memories_applied.append(m.id)
        return m

    async def run(self, inc: Incident) -> tuple[list[ActionResult], RunTrace]:
        trace = RunTrace(incident_id=inc.id)
        rec = Recorder(trace)
        restarts = 0
        while True:
            results: list[ActionResult] = []
            try:
                await self._batch(inc, rec, results)
                self.memory.save()
                return results, trace
            except ToolError as e:
                step_retry = self._use(rec, Guard.retry_failed_step_only) is not None
                if step_retry or restarts >= MAX_BATCH_RESTARTS:
                    self.memory.save()
                    results.append(
                        ActionResult(
                            kind=rec.step or "batch", ok=False, dry_run=False, detail=str(e)
                        )
                    )
                    return results, trace
                restarts += 1
                trace.batch_restarts += 1
                rec.attempt += 1

    async def _batch(self, inc: Incident, rec: Recorder, results: list[ActionResult]) -> None:
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
            return
        ticket = await self._run_step(rec, "ticket", lambda: self._ticket(inc, rec))
        results.append(ticket)
        if ticket.kind == "link_existing_ticket":
            return
        if inc.brief.confidence < self.min_conf:
            results.append(
                ActionResult(
                    kind="devin_session",
                    ok=True,
                    dry_run=True,
                    ref=None,
                    detail="skipped: confidence too low for an automated fix; human triage needed",
                )
            )
            return
        results.append(
            await self._run_step(rec, "fix_session", lambda: self._fix_session(inc, rec, ticket))
        )

    async def _run_step(
        self, rec: Recorder, name: str, fn: Callable[[], Awaitable[ActionResult]]
    ) -> ActionResult:
        rec.step = name
        attempt = 0
        while True:
            try:
                return await fn()
            except ToolError as e:
                policy = self._use(rec, Guard.retry_failed_step_only)
                if policy is None:
                    raise
                max_attempts = int(policy.params.get("max_attempts", 3))
                attempt += 1
                if not e.retryable or attempt >= max_attempts:
                    raise
                backoff = self._use(rec, Guard.retry_with_backoff, tool=e.code)
                base = float(backoff.params.get("base_seconds", 0.5)) if backoff else 0.0
                await self.sleep(base * (2 ** (attempt - 1)))
                rec.attempt += 1

    async def _ticket(self, inc: Incident, rec: Recorder) -> ActionResult:
        alert = inc.alert
        if self._use(rec, Guard.search_before_create) is not None:
            found: list[IssueRef] = await rec.call(
                "jira.search_issues",
                self.tools.jira_search_issues,
                summarize=lambda r: f"{len(r)} match(es)",
                project=self.project,
                service=alert.service,
                query=alert.title,
                error_signature=alert.error_signature,
            )
            if found:
                return ActionResult(
                    kind="link_existing_ticket",
                    ok=True,
                    dry_run=self.dry_run,
                    ref=found[0].key,
                    url=found[0].url,
                    detail=f"open issue already tracks this: {found[0].summary}",
                )

        issue_type = DEFAULT_ISSUE_TYPE
        if self._use(rec, Guard.resolve_issue_type, project=self.project) is not None:
            fact = self._use(rec, Guard.none, fact="issue_types", project=self.project)
            types = (
                list(fact.params["types"])
                if fact
                else await rec.call(
                    "jira.get_issue_types",
                    self.tools.jira_get_issue_types,
                    summarize=", ".join,
                    project=self.project,
                )
            )
            issue_type = _pick_issue_type(types)

        summary = f"[{alert.service}] {inc.brief.headline}"
        labels = ["sre-coworker", f"svc:{alert.service}"] + (
            [f"sig:{alert.error_signature}"] if alert.error_signature else []
        )
        try:
            issue: IssueRef = await rec.call(
                "jira.create_issue",
                self.tools.jira_create_issue,
                summarize=lambda r: r.key,
                project=self.project,
                issue_type=issue_type,
                summary=summary,
                description=describe_incident(inc),
                labels=labels,
            )
        except ToolError as e:
            if e.code != "INVALID_ISSUE_TYPE":
                raise
            # naive recovery: discover the valid types and try again
            types = await rec.call(
                "jira.get_issue_types",
                self.tools.jira_get_issue_types,
                summarize=", ".join,
                project=self.project,
            )
            issue = await rec.call(
                "jira.create_issue",
                self.tools.jira_create_issue,
                summarize=lambda r: r.key,
                project=self.project,
                issue_type=_pick_issue_type(types),
                summary=summary,
                description=describe_incident(inc),
                labels=labels,
            )
        return ActionResult(
            kind="jira_ticket",
            ok=True,
            dry_run=self.dry_run,
            ref=issue.key,
            url=issue.url,
            detail=summary,
        )

    async def _fix_session(
        self, inc: Incident, rec: Recorder, ticket: ActionResult
    ) -> ActionResult:
        prompt = inc.brief.fix_prompt
        facts = [m.text for m in self.memory.facts()]
        if facts:
            prompt += "\n\nStanding knowledge from previous incidents:\n" + "\n".join(
                f"- {f}" for f in facts
            )
        sess: SessionRef = await rec.call(
            "devin.create_session",
            self.tools.devin_create_session,
            summarize=lambda r: r.session_id,
            prompt=prompt,
            title=f"Fix {inc.alert.service}: {inc.brief.headline}",
            tags=["sre-coworker", inc.alert.service] + ([ticket.ref] if ticket.ref else []),
        )
        return ActionResult(
            kind="devin_session",
            ok=True,
            dry_run=self.dry_run,
            ref=sess.session_id,
            url=sess.url,
            detail=prompt,
        )


def _pick_issue_type(types: list[str]) -> str:
    for t in PREFERRED_ISSUE_TYPES:
        if t in types:
            return t
    return types[0] if types else DEFAULT_ISSUE_TYPE
