"""Execution trace: every tool call a run makes, in order, with outcome and cost.

The trace is the raw material for self-improvement (see reflect.py): procedures are
learned from what actually happened, not from what the code intended.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from sre_coworker.models import _now


class ToolError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable


class ToolCall(BaseModel):
    seq: int
    tool: str
    args: dict[str, Any]
    ok: bool
    error_code: str | None = None
    error: str | None = None
    result_summary: str | None = None
    duration_ms: float
    step: str | None = None
    attempt: int = 1


class RunTrace(BaseModel):
    run_id: str = Field(default_factory=lambda: f"run_{uuid4().hex[:8]}")
    incident_id: str
    calls: list[ToolCall] = Field(default_factory=list)
    batch_restarts: int = 0
    memories_applied: list[str] = Field(default_factory=list)
    started_at: Any = Field(default_factory=_now)

    @property
    def failures(self) -> list[ToolCall]:
        return [c for c in self.calls if not c.ok]

    def summary(self) -> str:
        return (
            f"{len(self.calls)} tool call(s), {len(self.failures)} failed, "
            f"{self.batch_restarts} batch restart(s), {len(self.memories_applied)} memories applied"
        )


class Recorder:
    """Wraps tool invocations so every call lands in the current trace."""

    def __init__(self, trace: RunTrace) -> None:
        self.trace = trace
        self.step: str | None = None
        self.attempt = 1

    async def call(
        self,
        tool: str,
        fn: Callable[..., Awaitable[Any]],
        summarize: Callable[[Any], str] | None = None,
        **args: Any,
    ) -> Any:
        t0 = time.perf_counter()
        seq = len(self.trace.calls) + 1
        try:
            result = await fn(**args)
        except ToolError as e:
            self.trace.calls.append(
                ToolCall(
                    seq=seq,
                    tool=tool,
                    args=args,
                    ok=False,
                    error_code=e.code,
                    error=e.message,
                    duration_ms=(time.perf_counter() - t0) * 1000,
                    step=self.step,
                    attempt=self.attempt,
                )
            )
            raise
        self.trace.calls.append(
            ToolCall(
                seq=seq,
                tool=tool,
                args=args,
                ok=True,
                result_summary=summarize(result) if summarize else None,
                duration_ms=(time.perf_counter() - t0) * 1000,
                step=self.step,
                attempt=self.attempt,
            )
        )
        return result
