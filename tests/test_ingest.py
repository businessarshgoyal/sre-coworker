import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from pathlib import Path

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from sre_coworker.config import Settings
from sre_coworker.connectors.deploys import FileDeploySource
from sre_coworker.consumer import consume
from sre_coworker.coworker import SRECoworker
from sre_coworker.ingest import Envelope
from sre_coworker.ingest.redis_stream import RedisStreamQueue
from sre_coworker.models import IncidentState

ROOT = Path(__file__).resolve().parents[1]
ALERT = json.loads((ROOT / "examples/alert_payments_5xx.json").read_text())


def make_coworker(**overrides: object) -> SRECoworker:
    s = Settings(
        runbooks_dir=ROOT / "runbooks",
        known_issues_file=ROOT / "examples/known_issues.json",
        **overrides,  # type: ignore[arg-type]
    )
    return SRECoworker(s, FileDeploySource(ROOT / "examples/deploys.json"))


class MemoryQueue:
    def __init__(self, items: list[tuple[str, dict[str, object]]]) -> None:
        self.items = items
        self.acked: list[str] = []
        self.nacked: list[str] = []

    async def messages(self) -> AsyncIterator[Envelope]:
        for i, (source, payload) in enumerate(self.items):
            mid = f"m{i}"

            async def ack(mid: str = mid) -> None:
                self.acked.append(mid)

            async def nack(mid: str = mid) -> None:
                self.nacked.append(mid)

            yield Envelope(source=source, payload=payload, ack=ack, nack=nack, message_id=mid)


async def test_consumer_acks_good_and_poison_messages_nacks_failures() -> None:
    q = MemoryQueue(
        [
            ("generic", ALERT),
            ("pagerduty", {"anything": 1}),  # unknown source -> poison, acked and dropped
            ("generic", {"title": "missing service"}),  # validation error -> nacked for retry
        ]
    )
    cw = make_coworker()
    handled = await consume(cw, q)
    assert handled == 3
    assert q.acked == ["m0", "m1"]
    assert q.nacked == ["m2"]
    assert len(cw.incidents) == 1
    assert next(iter(cw.incidents.values())).state == IncidentState.awaiting_approval


async def test_consumer_auto_approve_dispatches() -> None:
    cw = make_coworker(auto_approve_min_confidence=0.0)
    await consume(cw, MemoryQueue([("generic", ALERT)]), max_messages=1)
    inc = next(iter(cw.incidents.values()))
    assert inc.state == IncidentState.actions_dispatched
    assert inc.approved_by == "auto"


async def test_redis_stream_backend_round_trip() -> None:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    q = RedisStreamQueue("redis://unused", stream="alerts", block_ms=10)
    q.redis = fake
    await fake.xadd("alerts", {"source": "generic", "payload": json.dumps(ALERT)})

    cw = make_coworker()
    handled = await consume(cw, q, max_messages=1)
    assert handled == 1
    assert len(cw.incidents) == 1
    pending = await fake.xpending("alerts", "sre-coworker")
    assert pending["pending"] == 0  # acked


def test_webhook_disabled_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    from sre_coworker import api, config

    monkeypatch.setattr(config.settings, "webhook_secret", None)
    r = TestClient(api.app).post("/webhooks/generic", json=ALERT)
    assert r.status_code == 403


def test_webhook_requires_valid_hmac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    from sre_coworker import api, config

    monkeypatch.setattr(config.settings, "webhook_secret", "s3cret")
    client = TestClient(api.app)
    body = json.dumps(ALERT).encode()

    assert client.post("/webhooks/generic", content=body).status_code == 401
    bad = {"X-Signature-256": "sha256=" + "0" * 64}
    assert client.post("/webhooks/generic", content=body, headers=bad).status_code == 401

    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    r = client.post(
        "/webhooks/generic",
        content=body,
        headers={"X-Signature-256": sig, "content-type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["state"] == "awaiting_approval"
