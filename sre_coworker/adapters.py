"""Map vendor webhook payloads onto the normalised Alert model."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sre_coworker.models import Alert, Severity

_SEV = {
    "critical": Severity.critical,
    "error": Severity.high,
    "warning": Severity.medium,
    "info": Severity.low,
    "p1": Severity.critical,
    "p2": Severity.high,
    "p3": Severity.medium,
}


def _sev(raw: str | None) -> Severity:
    return _SEV.get((raw or "").lower(), Severity.high)


def from_datadog(p: dict[str, Any]) -> Alert:
    tags = dict(t.split(":", 1) for t in p.get("tags", "").split(",") if ":" in t)
    return Alert(
        source="datadog",
        service=tags.get("service") or p.get("service", "unknown"),
        title=p.get("title") or p.get("alert_title", "Datadog monitor"),
        description=p.get("body", ""),
        severity=_sev(p.get("priority") or p.get("alert_type")),
        fired_at=datetime.fromtimestamp(int(p.get("date", 0)) / 1000, tz=UTC)
        if p.get("date")
        else datetime.now(UTC),
        tags=tags,
        url=p.get("link"),
    )


def from_sentry(p: dict[str, Any]) -> Alert:
    ev = p.get("event", {}) or p.get("data", {}).get("event", {})
    tags = dict(ev.get("tags", []))
    return Alert(
        source="sentry",
        service=p.get("project") or tags.get("service", "unknown"),
        title=ev.get("title") or p.get("message", "Sentry issue"),
        description=ev.get("culprit", ""),
        severity=_sev(p.get("level") or ev.get("level")),
        error_signature=str(ev.get("fingerprint", [""])[0]) if ev.get("fingerprint") else None,
        tags=tags,
        url=p.get("url"),
    )


def from_generic(p: dict[str, Any]) -> Alert:
    return Alert.model_validate(p)


ADAPTERS = {"datadog": from_datadog, "sentry": from_sentry, "generic": from_generic}
