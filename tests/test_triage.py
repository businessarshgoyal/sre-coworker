import hashlib
import hmac
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sre_coworker.adapters import from_datadog
from sre_coworker.config import Settings
from sre_coworker.connectors.deploys import FileDeploySource
from sre_coworker.connectors.jira import load_known_issues
from sre_coworker.connectors.runbooks import load_runbooks
from sre_coworker.coworker import SRECoworker
from sre_coworker.models import Alert, IncidentState, Severity
from sre_coworker.triage import triage

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 9, 4, 9, 14, tzinfo=UTC)


def payments_alert() -> Alert:
    return Alert(
        source="datadog",
        service="payments-api",
        severity=Severity.critical,
        fired_at=T0,
        title="5xx spike on payments-api: checkout failing after declined card",
        description="Card retries looping with no timeout on POST /v1/charges",
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(
        runbooks_dir=ROOT / "runbooks",
        known_issues_file=ROOT / "examples/known_issues.json",
        dry_run=True,
    )


async def test_brief_blames_recent_related_deploy() -> None:
    brief = await triage(
        payments_alert(),
        FileDeploySource(ROOT / "examples/deploys.json"),
        load_runbooks(ROOT / "runbooks"),
        load_known_issues(ROOT / "examples/known_issues.json"),
    )
    assert brief.suspect_deploys[0].deploy.sha == "8b21f3a9c0"
    assert brief.runbooks[0].slug == "payments-5xx"
    assert not brief.is_duplicate  # ENG-600 is Done, must not count
    assert brief.confidence > 0.7
    assert "8b21f3a9c0" in brief.fix_prompt
    assert any(c.label == "deploy 8b21f3a9c0" for c in brief.citations)


async def test_error_signature_marks_duplicate_and_skips_ticket(settings: Settings) -> None:
    cw = SRECoworker(settings, FileDeploySource(ROOT / "examples/deploys.json"))
    alert = Alert(
        service="orders-api",
        title="orders export timeout",
        error_signature="orders.export.timeout",
        fired_at=T0,
    )
    inc = await cw.handle_alert(alert)
    assert inc.brief.is_duplicate
    assert inc.brief.known_issues[0].issue.key == "ENG-702"
    inc = await cw.approve(inc.id, "tester")
    assert [a.kind for a in inc.actions] == ["link_existing_ticket"]


async def test_low_confidence_creates_ticket_but_no_fix_session(settings: Settings) -> None:
    cw = SRECoworker(settings, FileDeploySource(ROOT / "examples/deploys.json"))
    inc = await cw.handle_alert(Alert(service="search-api", title="p99 latency", fired_at=T0))
    assert inc.brief.confidence < 0.5
    inc = await cw.approve(inc.id, "tester")
    kinds = {a.kind: a for a in inc.actions}
    assert kinds["jira_ticket"].dry_run
    assert kinds["devin_session"].ref is None


async def test_human_gate_blocks_until_approved(settings: Settings) -> None:
    cw = SRECoworker(settings, FileDeploySource(ROOT / "examples/deploys.json"))
    inc = await cw.handle_alert(payments_alert())
    assert inc.state == IncidentState.awaiting_approval
    assert inc.actions == []
    cw.reject(inc.id, "maya")
    with pytest.raises(ValueError):
        await cw.approve(inc.id, "maya")


def test_datadog_adapter() -> None:
    a = from_datadog(
        {
            "title": "[Triggered] 5xx spike",
            "priority": "P1",
            "date": 1788513240000,
            "tags": "service:payments-api,env:prod",
            "link": "https://dd/1",
        }
    )
    assert a.service == "payments-api"
    assert a.severity == Severity.critical
    assert a.fired_at == T0
    assert a.tags["env"] == "prod"


def test_api_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    from sre_coworker import config
    from sre_coworker.api import app

    monkeypatch.setattr(config.settings, "webhook_secret", "s3cret")
    client = TestClient(app)
    body = payments_alert().model_dump_json().encode()
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    r = client.post("/webhooks/generic", content=body, headers={"X-Signature-256": sig})
    assert r.status_code == 200
    inc = r.json()
    assert inc["state"] == "awaiting_approval"
    r = client.post(f"/incidents/{inc['id']}/approve", json={"by": "maya"})
    assert r.status_code == 200
    assert r.json()["state"] == "actions_dispatched"
    assert client.post(f"/incidents/{inc['id']}/approve", json={"by": "maya"}).status_code == 409
    r = client.post(
        "/webhooks/pagerduty",
        content=b"{}",
        headers={
            "X-Signature-256": "sha256=" + hmac.new(b"s3cret", b"{}", hashlib.sha256).hexdigest()
        },
    )
    assert r.status_code == 404
