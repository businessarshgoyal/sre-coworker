---
title: payments-api 5xx spike
services: [payments-api]
keywords: [5xx, checkout, timeout, retry, declined, card]
---

# payments-api 5xx spike

## Symptoms

- Error rate > 2% on `POST /v1/charges` for 5 minutes
- Checkout failures reported by support

## Diagnosis

- Compare the alert time against the last deploy of `payments-api`
- Check the card-retry loop: `retry_backoff_ms` must be > 0 and `max_retries` <= 3

## Remediation

1. If a deploy happened in the last 60 minutes, roll it back first
2. Verify `PAYMENTS_RETRY_BACKOFF_MS` is set in the env config
3. Confirm error rate recovers within 10 minutes; otherwise page the payments on-call
