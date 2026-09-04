"""Pull-based alert ingestion. The coworker subscribes to a queue; nothing listens on a port.

Message contract (all backends): JSON body with an optional `source` attribute/field
("datadog" | "sentry" | "generic", default "generic") and the vendor payload as the body.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class Envelope:
    source: str
    payload: dict[str, Any]
    ack: Callable[[], Awaitable[None]]
    nack: Callable[[], Awaitable[None]]
    message_id: str | None = None


class AlertQueue(Protocol):
    def messages(self) -> AsyncIterator[Envelope]: ...


def build_queue(backend: str, **kw: Any) -> AlertQueue:
    if backend == "pubsub":
        from sre_coworker.ingest.pubsub import PubSubQueue

        return PubSubQueue(**kw)
    if backend == "sqs":
        from sre_coworker.ingest.sqs import SQSQueue

        return SQSQueue(**kw)
    if backend == "redis":
        from sre_coworker.ingest.redis_stream import RedisStreamQueue

        return RedisStreamQueue(**kw)
    raise ValueError(f"unknown queue backend '{backend}'")
