from __future__ import annotations

import httpx

from sre_coworker.models import ActionResult, Incident


class DevinClient:
    """Thin wrapper over the Devin v1 API: https://docs.devin.ai/api-reference"""

    def __init__(self, api_key: str, base_url: str = "https://api.devin.ai/v1") -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    async def create_fix_session(self, incident: Incident, ticket_ref: str | None) -> ActionResult:
        payload = {
            "prompt": incident.brief.fix_prompt,
            "title": f"Fix {incident.alert.service}: {incident.brief.headline}",
            "tags": ["sre-coworker", incident.alert.service] + ([ticket_ref] if ticket_ref else []),
            "idempotent": True,
            "max_acu_limit": 10,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self.base_url}/sessions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            r.raise_for_status()
            data = r.json()
            return ActionResult(
                kind="devin_session",
                ok=True,
                dry_run=False,
                ref=data["session_id"],
                url=data["url"],
            )
