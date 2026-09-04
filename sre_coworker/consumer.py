from __future__ import annotations

import logging

from sre_coworker.adapters import ADAPTERS
from sre_coworker.coworker import SRECoworker
from sre_coworker.ingest import AlertQueue, Envelope
from sre_coworker.models import Incident

log = logging.getLogger(__name__)


async def handle_envelope(coworker: SRECoworker, env: Envelope) -> Incident | None:
    adapter = ADAPTERS.get(env.source)
    if adapter is None:
        log.warning("dropping message %s: unknown source %r", env.message_id, env.source)
        await env.ack()  # poison message; do not redeliver forever
        return None
    try:
        incident = await coworker.handle_alert(adapter(env.payload))
    except Exception:
        log.exception("triage failed for message %s; nacking", env.message_id)
        await env.nack()
        return None
    await env.ack()
    log.info(
        "incident %s (%s) confidence=%.2f state=%s",
        incident.id,
        incident.alert.service,
        incident.brief.confidence,
        incident.state.value,
    )
    return incident


async def consume(coworker: SRECoworker, queue: AlertQueue, max_messages: int | None = None) -> int:
    """Pull alerts until the queue iterator ends or `max_messages` were processed."""
    handled = 0
    async for env in queue.messages():
        await handle_envelope(coworker, env)
        handled += 1
        if max_messages is not None and handled >= max_messages:
            break
    return handled
