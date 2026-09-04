"""Reflect on a run's trace (and, later, its human-recorded outcome) and distill memories.

Each detector looks for a concrete waste pattern in the tool-call sequence and emits a
procedure or fact that would have avoided it. Memories are deduplicated by the store, so
the same lesson observed across many runs stays a single entry.
"""

from __future__ import annotations

from collections import Counter

from sre_coworker.memory import Guard, Memory, MemoryStore
from sre_coworker.models import Incident, Outcome
from sre_coworker.trace import RunTrace


def reflect_on_trace(trace: RunTrace, store: MemoryStore, project: str) -> list[Memory]:
    learned: list[Memory] = []
    calls = trace.calls

    # 1. create_issue rejected for an invalid type, then a get_issue_types call fixed it
    for i, c in enumerate(calls):
        if c.tool == "jira.create_issue" and c.error_code == "INVALID_ISSUE_TYPE":
            types_call = next(
                (x for x in calls[i + 1 :] if x.tool == "jira.get_issue_types" and x.ok), None
            )
            proc = Memory(
                kind="procedure",
                text=(
                    f"When creating Jira issues, first use `jira.get_issue_types` for project "
                    f"{project} and pass one of its valid IDs to `jira.create_issue`; issue types "
                    "valid elsewhere may be rejected."
                ),
                guard=Guard.resolve_issue_type,
                params={"project": project},
                learned_from=trace.run_id,
            )
            learned.append(store.add(proc))
            if types_call and types_call.result_summary:
                types = [t.strip() for t in types_call.result_summary.split(",") if t.strip()]
                fact = Memory(
                    kind="fact",
                    text=f"Jira project {project} accepts issue types: {', '.join(types)}.",
                    guard=Guard.none,
                    params={"fact": "issue_types", "project": project, "types": types},
                    learned_from=trace.run_id,
                )
                learned.append(store.add(fact))
            break

    # 2. a whole batch was rerun because one step failed -> earlier steps ran twice
    if trace.batch_restarts:
        repeated = [
            tool
            for tool, n in Counter(c.tool for c in calls if c.ok).items()
            if n > 1 and tool.startswith("jira.create")
        ]
        failed_tools = sorted(
            {c.tool for c in calls if not c.ok and c.error_code != "INVALID_ISSUE_TYPE"}
        )
        side_effects = f" (this run created {', '.join(repeated)} twice)" if repeated else ""
        learned.append(
            store.add(
                Memory(
                    kind="procedure",
                    text=(
                        f"If a step fails partway through dispatch ({', '.join(failed_tools)}), "
                        "retry only the failed tool call rather than rerunning the whole "
                        f"batch{side_effects}."
                    ),
                    guard=Guard.retry_failed_step_only,
                    params={"max_attempts": 3},
                    learned_from=trace.run_id,
                )
            )
        )

    # 3. a retryable error that later succeeded on the same tool -> back off and retry it
    for c in calls:
        if not c.ok and c.error_code in {"RATE_LIMITED", "TIMEOUT", "UNAVAILABLE"}:
            if any(x.tool == c.tool and x.ok and x.seq > c.seq for x in calls):
                learned.append(
                    store.add(
                        Memory(
                            kind="procedure",
                            text=(
                                f"`{c.tool}` returns {c.error_code} transiently; retry it with "
                                "exponential backoff instead of failing the run."
                            ),
                            guard=Guard.retry_with_backoff,
                            params={"tool": c.error_code, "base_seconds": 0.5},
                            learned_from=trace.run_id,
                        )
                    )
                )
                break

    return [m for m in learned if m.learned_from == trace.run_id]


def reflect_on_outcome(
    inc: Incident, outcome: Outcome, trace: RunTrace | None, store: MemoryStore
) -> list[Memory]:
    learned: list[Memory] = []
    created = trace is not None and any(c.tool == "jira.create_issue" and c.ok for c in trace.calls)
    if outcome.duplicate_of and created:
        learned.append(
            store.add(
                Memory(
                    kind="procedure",
                    text=(
                        "Before filing a Jira bug with `jira.create_issue`, use "
                        "`jira.search_issues` (same service, error signature or title overlap) "
                        f"to check for an existing open issue; run {trace.run_id if trace else ''} "
                        f"filed a duplicate of {outcome.duplicate_of}."
                    ),
                    guard=Guard.search_before_create,
                    learned_from=trace.run_id if trace else inc.id,
                )
            )
        )
    return learned
