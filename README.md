# sre-coworker

An SRE triage coworker: **alert → root-cause brief → human approval → Jira ticket → Devin session that drafts the fix PR**.

The triage is deterministic and citation-backed (no LLM in the loop): every claim in the brief points at a deploy, a runbook, or an existing ticket. The expensive, judgment-heavy part — actually writing the fix — is delegated to Devin via its API.

```
Datadog / Sentry / generic webhook
        │
        ▼
  normalise Alert ──► correlate recent deploys (GitHub commits or a JSON file)
                  ──► match runbooks (markdown + front-matter, ./runbooks)
                  ──► dedupe against open tickets (Jira JQL or a JSON file)
        │
        ▼
  Brief { likely_cause, confidence, suspect_deploys, runbooks, known_issues,
          recommended_actions, fix_prompt, citations }
        │
        ▼   POST /incidents/{id}/approve      (skipped for duplicates)
  Jira ticket  ──►  Devin session (prompt = fix_prompt)  ──►  PR
```

Guardrails:

- Nothing is dispatched until a human approves (`auto_approve_min_confidence` can relax this).
- Duplicates of an open ticket link to it instead of opening a new one or paging anyone.
- Below `min_confidence_for_fix_session` (default 0.5) a ticket is opened but no Devin session is started.
- Every connector is optional; missing credentials mean that action runs in dry-run and shows exactly what it would have sent.

## Quick start

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"

# triage a sample alert, stop at the approval gate
sre-coworker triage examples/alert_payments_5xx.json

# approve and dispatch (dry-run: prints the Jira payload and the Devin prompt)
sre-coworker triage examples/alert_payments_5xx.json --approve

# a recurrence of a known issue -> no new ticket
sre-coworker triage examples/alert_orders_timeout.json --approve

# raw vendor webhook
sre-coworker triage examples/datadog_webhook.json --source datadog
```

Run as a service:

```bash
sre-coworker serve --port 8000
curl -X POST localhost:8000/webhooks/datadog -H 'content-type: application/json' \
     -d @examples/datadog_webhook.json
curl -X POST localhost:8000/incidents/<id>/approve -d '{"by":"maya"}' -H 'content-type: application/json'
```

## Going live

Copy `.env.example` to `.env`, set `SRE_DRY_RUN=false`, and fill in whichever connectors you want:

| Connector | Env vars | Behaviour when unset |
|---|---|---|
| Deploys | `SRE_GITHUB_REPO`, `SRE_GITHUB_TOKEN` | reads `examples/deploys.json` |
| Tickets | `SRE_JIRA_BASE_URL`, `SRE_JIRA_EMAIL`, `SRE_JIRA_API_TOKEN`, `SRE_JIRA_PROJECT_KEY` | reads `examples/known_issues.json`, dry-run ticket |
| Fix PR | `SRE_DEVIN_API_KEY` | dry-run session; prompt printed |

The Devin call is `POST https://api.devin.ai/v1/sessions` with `idempotent: true`, tagged `sre-coworker` and the ticket key, capped at `max_acu_limit: 10` ([API reference](https://docs.devin.ai/api-reference/sessions/create-a-new-devin-session)).

## Adding a runbook

Drop a markdown file in `runbooks/` with front-matter; steps under a `## Remediation` heading are lifted into the brief and the Devin prompt.

```markdown
---
title: payments-api 5xx spike
services: [payments-api]
keywords: [5xx, checkout, timeout, retry]
---
## Remediation
1. Roll back the last deploy if it is < 60 min old
2. Verify PAYMENTS_RETRY_BACKOFF_MS is set
```

## Development

```bash
pytest
ruff check . && ruff format --check .
mypy sre_coworker tests
```

## Roadmap

- Slack approval buttons instead of the REST gate
- PagerDuty / Grafana adapters
- Poll the Devin session and post the PR link back to the Jira ticket
- Learn from approve/reject decisions to tune scoring weights per service
