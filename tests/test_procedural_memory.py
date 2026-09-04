"""Procedural self-improvement: run 1's trace teaches run 2 to make fewer, better tool calls."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sre_coworker.cases import StaticDeploys
from sre_coworker.config import Settings
from sre_coworker.coworker import SRECoworker
from sre_coworker.memory import Guard, MemoryStore
from sre_coworker.models import Alert, Deploy, Outcome
from sre_coworker.tools import DryRunTools

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _deploys() -> list[Deploy]:
    return [
        Deploy(
            sha="culprit00",
            message="orders: rewrite pagination cursor encoding",
            author="a",
            deployed_at=NOW - timedelta(minutes=30),
            files=["orders_api/pagination.py"],
        )
    ]


def _alert(n: int) -> Alert:
    return Alert(
        service="orders-api",
        title="orders-api 5xx spike on list endpoint",
        description="cursor decode errors in pagination",
        error_signature=f"CursorDecodeError#{n}",
        fired_at=NOW + timedelta(minutes=n),
    )


def _coworker(tmp_path: Path, tools: DryRunTools) -> SRECoworker:
    s = Settings(
        runbooks_dir=ROOT / "runbooks",
        known_issues_file=tmp_path / "none.json",
        cases_dir=tmp_path / "cases",
        memory_file=tmp_path / "memory.json",
        auto_approve_min_confidence=0.0,
    )
    cw = SRECoworker(s, StaticDeploys(_deploys()), tools=tools)

    async def no_sleep(_: float) -> None:
        return None

    cw.executor.sleep = no_sleep
    return cw


async def test_run_two_makes_fewer_calls_and_no_failures(tmp_path: Path) -> None:
    # Jira project only accepts "Incident"; Devin 429s on its first call each run.
    tools = DryRunTools(
        project="ENG", issue_types={"ENG": ["Incident"]}, devin_transient_failures=1
    )

    # ---- run 1: no memory; naive order, naive whole-batch retry ----
    cw = _coworker(tmp_path, tools)
    inc1 = await cw.handle_alert(_alert(1))
    t1 = cw.traces[inc1.id]
    tools_called_1 = [c.tool for c in t1.calls]

    assert t1.memories_applied == []
    assert tools_called_1 == [
        "jira.create_issue",  # INVALID_ISSUE_TYPE: guessed "Bug"
        "jira.get_issue_types",
        "jira.create_issue",  # ok
        "devin.create_session",  # RATE_LIMITED
        "jira.create_issue",  # whole batch rerun: same wrong guess again...
        "jira.get_issue_types",
        "jira.create_issue",  # ...and a duplicate ticket
        "devin.create_session",  # ok
    ]
    assert len(t1.failures) == 3
    assert t1.batch_restarts == 1
    assert [c.tool for c in t1.calls if c.tool == "jira.create_issue" and c.ok].count(
        "jira.create_issue"
    ) == 2, "run 1 wasted a call and created two tickets for one incident"

    learned = {m.guard for m in cw.last_learned}
    assert learned == {
        Guard.resolve_issue_type,
        Guard.none,  # fact: valid issue types for ENG
        Guard.retry_failed_step_only,
        Guard.retry_with_backoff,
    }
    assert all(m.learned_from == t1.run_id for m in cw.last_learned)

    # ---- run 2: fresh process, same memory file, same (still flaky) tools ----
    tools.devin_transient_failures = 1
    cw2 = _coworker(tmp_path, tools)
    inc2 = await cw2.handle_alert(_alert(2))
    t2 = cw2.traces[inc2.id]

    assert [c.tool for c in t2.calls] == [
        "jira.create_issue",  # valid type from the remembered fact; no discovery call
        "devin.create_session",  # RATE_LIMITED
        "devin.create_session",  # only the failed step retried
    ]
    assert [c.ok for c in t2.calls] == [True, False, True]
    assert t2.batch_restarts == 0
    assert len(t2.calls) < len(t1.calls)
    assert len(t2.failures) < len(t1.failures)
    assert len(t2.memories_applied) == 4
    assert cw2.last_learned == [], "nothing new to learn: the run went as the memory predicted"
    assert [a.kind for a in inc2.actions] == ["jira_ticket", "devin_session"]
    assert all(a.ok for a in inc2.actions)

    # memory is durable and auditable
    store = MemoryStore(tmp_path / "memory.json")
    assert {m.id for m in store.items} == {m.id for m in cw.last_learned}
    assert all(m.times_applied == 1 for m in store.items)


async def test_duplicate_outcome_teaches_search_before_create(tmp_path: Path) -> None:
    tools = DryRunTools(project="ENG", issue_types={"ENG": ["Bug"]})
    cw = _coworker(tmp_path, tools)

    inc1 = await cw.handle_alert(_alert(1))
    inc2 = await cw.handle_alert(_alert(1))  # same signature; nothing stopped a second ticket
    key1, key2 = inc1.actions[0].ref, inc2.actions[0].ref
    assert key1 and key2 and key1 != key2

    # on-call marks incident 2 as a duplicate of incident 1's ticket
    cw.record_outcome(inc2.id, Outcome(duplicate_of=key1, recorded_by="oncall"))
    assert [m.guard for m in cw.last_learned] == [Guard.search_before_create]
    assert cw.last_learned[0].learned_from == cw.traces[inc2.id].run_id

    # run 3 searches first and links instead of creating a third ticket
    inc3 = await cw.handle_alert(_alert(1))
    t3 = cw.traces[inc3.id]
    assert [c.tool for c in t3.calls] == ["jira.search_issues"]
    assert inc3.actions[0].kind == "link_existing_ticket"
    assert inc3.actions[0].ref == key1


async def test_disabled_memory_is_not_applied(tmp_path: Path) -> None:
    tools = DryRunTools(project="ENG", issue_types={"ENG": ["Incident"]})
    cw = _coworker(tmp_path, tools)
    await cw.handle_alert(_alert(1))
    for m in cw.memory.items:
        m.enabled = False
    cw.memory.save()

    cw2 = _coworker(tmp_path, tools)
    inc = await cw2.handle_alert(_alert(2))
    t = cw2.traces[inc.id]
    assert t.memories_applied == []
    assert t.calls[0].error_code == "INVALID_ISSUE_TYPE"
