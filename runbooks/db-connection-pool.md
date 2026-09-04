---
title: Database connection pool exhaustion
services: [payments-api, orders-api, users-api]
keywords: [connection, pool, exhausted, postgres, too many clients]
---

# Database connection pool exhaustion

## Symptoms

- `FATAL: too many clients already` in service logs
- Latency climbs on every endpoint that touches Postgres

## Remediation

1. Check pgbouncer `SHOW POOLS` for the saturated pool
2. Kill idle-in-transaction sessions older than 5 minutes
3. If a deploy increased worker count, revert it
