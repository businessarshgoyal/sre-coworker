from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sre_coworker.adapters import ADAPTERS
from sre_coworker.config import settings
from sre_coworker.coworker import SRECoworker
from sre_coworker.models import Incident

app = typer.Typer(help="SRE coworker: triage an alert, review the brief, dispatch actions.")
console = Console()


def render(inc: Incident) -> None:
    b = inc.brief
    console.print(
        Panel(
            f"[bold]{b.headline}[/bold]\n{b.summary}\n\n"
            f"[yellow]Likely cause:[/yellow] {b.likely_cause}\n"
            f"[yellow]Confidence:[/yellow] {b.confidence:.0%}"
            + ("   [red]DUPLICATE[/red]" if b.is_duplicate else ""),
            title=f"Incident {inc.id} · {inc.state.value}",
        )
    )
    if b.suspect_deploys:
        t = Table(title="Suspect deploys")
        t.add_column("sha")
        t.add_column("message")
        t.add_column("min before")
        t.add_column("score")
        for d in b.suspect_deploys:
            t.add_row(
                d.deploy.sha, d.deploy.message, f"{d.minutes_before_alert:.0f}", f"{d.score:.2f}"
            )
        console.print(t)
    if b.known_issues:
        t = Table(title="Similar open tickets")
        t.add_column("key")
        t.add_column("summary")
        t.add_column("reason")
        for k in b.known_issues:
            t.add_row(k.issue.key, k.issue.summary, k.reason)
        console.print(t)
    if b.runbooks:
        console.print(
            "[bold]Runbooks:[/bold] " + ", ".join(f"{r.title} ({r.score:.2f})" for r in b.runbooks)
        )
    console.print("[bold]Recommended actions:[/bold]")
    for i, a in enumerate(b.recommended_actions, 1):
        console.print(f"  {i}. {a}")
    console.print("[bold]Sources:[/bold]")
    for c in b.citations:
        console.print(f"  - {c.label}: {c.url or c.excerpt or ''}")
    if inc.actions:
        console.print("[bold]Actions dispatched:[/bold]")
        for act in inc.actions:
            tag = "[dim](dry-run)[/dim]" if act.dry_run else "[green](live)[/green]"
            console.print(f"  - {act.kind} {tag} ref={act.ref} {act.url or ''}")
            if act.kind == "devin_session" and act.dry_run:
                console.print(Panel(act.detail or "", title="Devin prompt (would be sent)"))


@app.command()
def triage(
    alert_file: Annotated[Path, typer.Argument(help="JSON alert payload")],
    source: Annotated[str, typer.Option(help="generic | datadog | sentry")] = "generic",
    approve: Annotated[
        bool, typer.Option("--approve", help="Skip the human gate and dispatch actions")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Triage one alert file and print the brief (dry-run unless SRE_DRY_RUN=false)."""
    payload = json.loads(alert_file.read_text())
    alert = ADAPTERS[source](payload)

    async def run() -> Incident:
        cw = SRECoworker(settings)
        inc = await cw.handle_alert(alert)
        if approve:
            inc = await cw.approve(inc.id, approved_by="cli")
        return inc

    inc = asyncio.run(run())
    if as_json:
        console.print_json(inc.model_dump_json())
    else:
        render(inc)


@app.command()
def consume(
    backend: Annotated[
        str | None, typer.Option(help="pubsub | sqs | redis (default: SRE_QUEUE_BACKEND)")
    ] = None,
    max_messages: Annotated[
        int | None, typer.Option(help="Stop after N messages (default: run forever)")
    ] = None,
    auto_approve: Annotated[
        bool, typer.Option("--auto-approve", help="Dispatch actions without a human gate")
    ] = False,
) -> None:
    """Pull alerts from a queue. No inbound port is opened."""
    import logging

    from sre_coworker.consumer import consume as run_consumer
    from sre_coworker.ingest import build_queue

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = settings.model_copy(update={"queue_backend": backend or settings.queue_backend})
    if not cfg.queue_backend:
        raise typer.BadParameter("set --backend or SRE_QUEUE_BACKEND")
    queue_backend: str = cfg.queue_backend
    if auto_approve:
        cfg = cfg.model_copy(update={"auto_approve_min_confidence": 0.0})
    queue = build_queue(queue_backend, **cfg.queue_kwargs())
    console.print(f"[bold]consuming from {cfg.queue_backend}[/bold] (dry_run={cfg.dry_run})")
    handled = asyncio.run(run_consumer(SRECoworker(cfg), queue, max_messages))
    console.print(f"processed {handled} message(s)")


@app.command()
def publish(
    alert_file: Annotated[Path, typer.Argument(help="JSON alert payload")],
    source: Annotated[str, typer.Option(help="generic | datadog | sentry")] = "generic",
) -> None:
    """Publish a sample alert to the configured Redis stream (local testing helper)."""
    import redis

    r = redis.Redis.from_url(settings.redis_url)
    msg_id = r.xadd(settings.redis_stream, {"source": source, "payload": alert_file.read_text()})
    console.print(f"published {msg_id!r} to {settings.redis_stream}")


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Run the webhook server."""
    import uvicorn

    uvicorn.run("sre_coworker.api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    app()
