# Verification Result Cache

## Goals

The cache reuses recent, high-confidence email verdicts without turning old or
temporary receiver behavior into a current answer. It also coalesces concurrent
requests for the same address so only one live probe reaches the receiver.

This feature changes backend scheduling and persistence only. It does not alter
frontend assets or SMTP result display text.

## Freshness Policy

| Result | Fresh TTL | Promotion rule |
| --- | ---: | --- |
| First deliverable confirmation | 7 days | One live confirmation |
| Repeated deliverable confirmation | 14 days | At least two confirmations spanning 1 day |
| Stable deliverable confirmation | 30 days | At least two confirmations spanning 7 days |
| Explicit permanent SMTP or Microsoft API rejection | 3 days | Terminal negative evidence |
| Mailbox full | 2 hours | Explicit quota/full signal |

Catch-all results, catch-all conflicts, SMTP 4XX, greylisting, timeouts,
throttling, connection failures, and DNS/MX uncertainty do not become final
email-level cache entries. Expired rows remain available for 90 days for
history and metrics, but cache lookup never returns them as current verdicts.

## Request Flow

1. Submission performs one batched cache lookup.
2. Fresh hits are persisted immediately in the visible task.
3. Workers receive only pending misses.
4. `verification_probe_leases` elects one live-probe owner per email.
5. Duplicate tasks wait in `verification_probe_waiters` without consuming a
   worker slot.
6. A cacheable result fans out to every waiter. A non-cacheable result releases
   waiters so they can run normally.
7. Worker heartbeats and incremental result callbacks renew probe leases.
8. Worker failure or lease expiry releases only the affected probe addresses.

Frequently requested deliverable entries within 24 hours of expiry can create
a bounded internal refresh task. Refresh tasks have no guest token, CSV,
notification, result object, or automatic retry. They sort behind user tasks,
and at most 100 top-level refresh tasks may be queued at once.

## Schema And Release

`scripts/ensure_verification_cache_schema.py` runs during the Shanghai release
before service restart. It creates additive columns/tables, performs the
history backfill once under migration key `verification_cache_schema_version`,
and excludes cached hits, catch-all evidence, and temporary outcomes from the
backfill. Deploy Shanghai before Hong Kong for every schema-changing release.

The daily retention service deletes a cache row only after
`stale_expires_at`; freshness remains controlled by `expires_at`.

## Observability

`GET /api/admin/metrics` includes `verification_cache` with:

- lookups, fresh hits, misses, stale rows seen, and hit rate;
- deliverable, permanent-invalid, and mailbox-full writes;
- duplicate requests coalesced and refresh tasks scheduled;
- current retained/fresh rows by outcome class.

Metrics are daily aggregates and contain no email address. Local benchmark
guardrail: 5,000-hit lookup P95 below 500ms and 5,000-miss lookup P95 below
250ms in `tests/verification_cache_benchmark.py`. Counters are aggregated in
memory and flushed every 5 seconds, so telemetry does not add a PostgreSQL
write to each verification request.

## Verification Checklist

1. Run `tests/verification_cache_smoke.py` and schema/benchmark smoke tests.
2. Deploy Shanghai and confirm the migration marker and cache row counts.
3. Verify a repeated email completes from cache without a worker lease.
4. Verify a mixed batch sends only misses to the remote worker.
5. Submit the same uncached email concurrently and confirm one live probe.
6. Expire a cache entry and confirm a new live verification occurs.
7. Deploy Hong Kong, then verify local, CloudShell, CloudStudio, QQ, and Outlook
   worker paths.
