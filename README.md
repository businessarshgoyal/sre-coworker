# sre-coworker

An SRE triage coworker: **alert → root-cause brief → human approval → Jira ticket → Devin session that drafts the fix PR**.

The triage is deterministic and citation-backed (no LLM in the loop): every claim in the brief points at a deploy, a runbook, or an existing ticket. The expensive, judgment-heavy part — actually writing the fix — is delegated to Devin via its API.

```
Datadog / Sentry / your pipeline
        │  publish to Pub/Sub · SNS→SQS · Redis Stream   (pull-based, no inbound port)
        ▼
  sre-coworker consume  ──► normalise Alert ──► correlate recent deploys (GitHub commits or a JSON file)
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

## Ingestion: pull from a queue (recommended)

The coworker *subscribes*; it never has to accept inbound HTTP. Auth is the cloud IAM of the queue, and delivery is at-least-once with ack/nack (unknown sources are acked as poison, triage failures are nacked for redelivery).

| Backend | Install | Env | Route alerts in |
|---|---|---|---|
| Google Pub/Sub | `pip install -e ".[pubsub]"` | `SRE_QUEUE_BACKEND=pubsub SRE_PUBSUB_PROJECT=… SRE_PUBSUB_SUBSCRIPTION=…` | Datadog/Sentry → Cloud Function or Eventarc → topic; or publish from your own pipeline |
| AWS SQS | `pip install -e ".[sqs]"` | `SRE_QUEUE_BACKEND=sqs SRE_SQS_QUEUE_URL=… SRE_SQS_REGION=…` | Datadog and Sentry both ship native SNS integrations → SNS → SQS (SNS envelopes are unwrapped) |
| Redis Streams | `pip install -e ".[redis]"` | `SRE_QUEUE_BACKEND=redis SRE_REDIS_URL=… SRE_REDIS_STREAM=alerts` | `XADD alerts * source datadog payload '{…}'` |

Message contract: body is the vendor JSON; a `source` attribute (`datadog` | `sentry` | `generic`, default `generic`) selects the adapter. Pub/Sub: message attribute. SQS: message attribute or SNS message attribute. Redis: stream field.

```bash
sre-coworker consume --backend redis                  # runs forever, human approval via REST
sre-coworker consume --backend pubsub --auto-approve  # no gate: ticket + fix session on every alert

# local demo
docker run -d --rm -p 6379:6379 redis:7-alpine
sre-coworker publish examples/alert_payments_5xx.json
SRE_QUEUE_BACKEND=redis sre-coworker consume --max-messages 1
```

## Ingestion: signed webhook (optional)

Disabled unless `SRE_WEBHOOK_SECRET` is set. Every request must carry `X-Signature-256: sha256=<hex HMAC-SHA256 of the raw body>` (constant-time compare). Use this only when a vendor cannot publish to a queue.

```bash
SRE_WEBHOOK_SECRET=s3cret sre-coworker serve --port 8000
body=$(cat examples/datadog_webhook.json)
sig=$(printf '%s' "$body" | openssl dgst -sha256 -hmac s3cret | awk '{print $2}')
curl -X POST localhost:8000/webhooks/datadog -H "X-Signature-256: sha256=$sig" \
     -H 'content-type: application/json' -d "$body"
curl -X POST localhost:8000/incidents/<id>/approve -d '{"by":"maya"}' -H 'content-type: application/json'
```

The approve/reject REST endpoints are still exposed by `serve`; put them behind your SSO proxy or replace with Slack buttons (roadmap).

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

### Incident regression suite

`tests/regression/cases/*.yaml` is a scenario matrix: each file fixes an alert (or raw
Datadog/Sentry payload), recent deploys and open known issues, and asserts what the brief
must conclude (top suspect deploy, duplicate detection, confidence band, runbook, dispatched
actions). When triage gets a real incident wrong, add a case reproducing it, then fix
`triage.py` until `pytest tests/regression` passes.

## Learning loop (how it improves per run)

Triage is deterministic, but the numbers it reasons with live in `weights.yaml` and the
ground truth lives in the case set. Each resolved incident feeds both:

1. **Record the outcome** once the incident is closed — what the culprit actually was,
   which suspect was innocent, whether it was a duplicate, whether a fix session was needed:
   ```bash
   curl -X POST localhost:8000/incidents/<id>/outcome \
     -d '{"culprit_sha":"8b21f3a9c0","innocent_shas":["41ddc0e772"],"recorded_by":"oncall"}'
   ```
   This writes `tests/regression/cases/learned_<ts>_<service>_<id>.yaml` with the exact
   alert, deploys and open tickets that were seen, plus the asserted outcome. From then on
   `pytest` fails if triage ever gets that incident shape wrong again.
2. **Re-fit the weights** against the whole case set:
   ```bash
   sre-coworker tune            # report: cases passing before -> after, which weights moved
   sre-coworker tune --write    # persist the best weights.yaml
   ```
   `tune` hill-climbs the numeric weights (recency vs. keyword overlap, service bonus,
   dedupe threshold, ...) maximising passing cases, tie-broken by staying closest to the
   current weights, so nothing drifts without a case to justify it. Exit code 1 if any case
   still fails — that means the rule *structure* needs a code change, not just a number.

Every change is a reviewable diff (a YAML case + a weights delta), which is the point: the
coworker gets better with each incident without a model whose behaviour you can't audit.

## Roadmap

- Slack approval buttons instead of the REST gate
- PagerDuty / Grafana adapters; Kafka and Azure Service Bus queue backends
- Dead-letter handling after N nacks
- Poll the Devin session and post the PR link back to the Jira ticket
- Learn from approve/reject decisions to tune scoring weights per service
