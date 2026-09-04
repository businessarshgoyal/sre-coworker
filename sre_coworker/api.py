from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from sre_coworker.adapters import ADAPTERS
from sre_coworker.config import settings
from sre_coworker.coworker import SRECoworker
from sre_coworker.models import Incident

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
        "runbooks": [rb.slug for rb in coworker.runbooks],
    }


@app.post("/webhooks/{source}", response_model=Incident)
async def webhook(source: str, payload: dict[str, Any]) -> Incident:
    adapter = ADAPTERS.get(source)
    if adapter is None:
        raise HTTPException(404, f"unknown alert source '{source}'")
    return await coworker.handle_alert(adapter(payload))


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
