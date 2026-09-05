# Provider request budget

Codex Screener protects a local Unusual Whales request budget before every
outbound HTTP transport attempt. The API Basic policy has a **40,000-attempt
cap** and a **7,000-attempt protected reserve**, leaving at most **33,000
attempts** for collection and analysis.

The accounting window is a trailing **24-hour** period ending when usage is
read or an attempt is reserved. This is conservative relative to a calendar-day
reset and does **not** infer the provider's billing boundary. Provider headers
are retained only as response metadata; they never reset, refund, or reconcile
this local counter.

Provider entitlements and reset schedules can change. Confirm the active plan
in the provider account before changing these local limits. The local policy is
independent of the provider plan and does not increase an account entitlement.

The budget charges immediately before the HTTP transport runs. Successful
requests, HTTP errors, retry attempts, connection failures, and process failure
after reservation all consume one local attempt. Once 33,000 local attempts are
inside the rolling window, later provider calls fail before a request is sent.

The SQLite ledger uses the owner-private runtime root by default. Override it
with `CODEX_SCREENER_PROVIDER_USAGE_DATABASE`; the legacy
`MORNING_EDGE_PROVIDER_USAGE_DATABASE` name remains supported. The ledger
directory and its main, WAL, and SHM files are owner-private (`0700` directory,
`0600` files). The ledger stores provider name, timestamps, and safe evidence
identifiers only. It does not store API keys, authorization headers, request
URLs, response bodies, or market data.

Inspect local usage without a provider call:

```sh
PYTHONPATH=src python3 -m morning_edge.cli provider-usage
```

## Baseline reconciliation

Pre-ledger live activity must never be fabricated as transport rows. Record it
once as an immutable, auditable adjustment. Repeating the same command is safe:
the adjustment ID is idempotent only if every supplied value is identical;
conflicting reuse fails.

Use a unique adjustment ID and values supported by the operator's own usage
records. The command below is an example only:

```sh
PYTHONPATH=src python3 -m morning_edge.cli provider-baseline-adjust \
  --adjustment-id provider-preledger-YYYYMMDD \
  --attempted-requests 100 \
  --evidence-id account-usage-reconciliation-YYYYMMDD \
  --effective-at YYYY-MM-DDT00:00:00+00:00
```

Replaying an identical adjustment is idempotent and makes no provider request.
The adjustment counts against the local reserve while its effective timestamp
remains inside the trailing 24-hour window.

## Collection limits

`historical-backfill --max-items` caps logical collection items, not HTTP
attempts. Each item can make up to three transport attempts because GET retries
are bounded at three. A live command is rejected before provider I/O if its
maximum transport attempts would exceed the remaining local capacity. Its output
reports the logical-item cap, maximum transport attempts, and remaining capacity
before the run.

Historical backfill has one explicit operator override:
`--authorized-reserve-floor N`. It is accepted only with `--live` and
`--audit-accepted`, applies only to that process, and must be smaller than the
40,000-request cap. The result records the authorized floor and whether it is
below the normal 7,000-request reserve. Other commands continue to use the
normal reserve unless they receive separate explicit authorization.

Normal live CLI commands pass the same configured ledger into the provider
client. A real `UnusualWhalesClient` now requires that explicit shared budget;
it cannot create a relative-path fallback ledger. Offline injected transports
remain available for deterministic tests.
