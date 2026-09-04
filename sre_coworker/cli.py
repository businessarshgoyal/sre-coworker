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
def serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Run the webhook server."""
    import uvicorn

    uvicorn.run("sre_coworker.api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    app()
