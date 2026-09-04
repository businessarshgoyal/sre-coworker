from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from sre_coworker.adapters import ADAPTERS
from sre_coworker.config import settings
from sre_coworker.coworker import SRECoworker
from sre_coworker.models import Incident, Outcome

app = FastAPI(title="SRE Coworker", version="0.1.0")
coworker = SRECoworker(settings)


class Decision(BaseModel):
    by: str = "unknown"


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "dry_run": settings.dry_run,
        "jira_live": settings.jira_live,
        "devin_live": settings.devin_live,
        "webhook_enabled": bool(settings.webhook_secret),
        "queue_backend": settings.queue_backend,
        "runbooks": [rb.slug for rb in coworker.runbooks],
    }


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Expects `X-Signature-256: sha256=<hex hmac of raw body>`."""
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))


@app.post("/webhooks/{source}", response_model=Incident)
async def webhook(
    source: str,
    request: Request,
    x_signature_256: str | None = Header(default=None),
) -> Incident:
    """Push ingestion. Disabled unless SRE_WEBHOOK_SECRET is set; prefer `sre-coworker consume`."""
    if not settings.webhook_secret:
        raise HTTPException(403, "webhook ingestion disabled; use a queue backend")
    body = await request.body()
    if not verify_signature(settings.webhook_secret, body, x_signature_256):
        raise HTTPException(401, "invalid signature")
    adapter = ADAPTERS.get(source)
    if adapter is None:
        raise HTTPException(404, f"unknown alert source '{source}'")
    return await coworker.handle_alert(adapter(json.loads(body)))


@app.get("/incidents", response_model=list[Incident])
async def list_incidents() -> list[Incident]:
    return list(coworker.incidents.values())


@app.get("/incidents/{incident_id}", response_model=Incident)
async def get_incident(incident_id: str) -> Incident:
    inc = coworker.incidents.get(incident_id)
    if inc is None:
        raise HTTPException(404, "incident not found")
    return inc


@app.post("/incidents/{incident_id}/approve", response_model=Incident)
async def approve(incident_id: str, decision: Decision) -> Incident:
    if incident_id not in coworker.incidents:
        raise HTTPException(404, "incident not found")
    try:
        return await coworker.approve(incident_id, decision.by)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/incidents/{incident_id}/reject", response_model=Incident)
async def reject(incident_id: str, decision: Decision) -> Incident:
    if incident_id not in coworker.incidents:
        raise HTTPException(404, "incident not found")
    return coworker.reject(incident_id, decision.by)


class OutcomeRecorded(BaseModel):
    incident: Incident
    case_file: str


@app.post("/incidents/{incident_id}/outcome", response_model=OutcomeRecorded)
async def record_outcome(incident_id: str, outcome: Outcome) -> OutcomeRecorded:
    """Record what actually happened; becomes a regression case the triage rules must satisfy."""
    if incident_id not in coworker.incidents:
        raise HTTPException(404, "incident not found")
    try:
        path = coworker.record_outcome(incident_id, outcome)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return OutcomeRecorded(incident=coworker.incidents[incident_id], case_file=str(path))
