# Architecture

Codex Screener makes every displayed conclusion traceable to timestamped evidence.
It does not treat options flow, dark-pool prints, GEX, news, or technical
indicators as a trading instruction by itself.

```text
Read-only provider response
        ↓
Raw evidence capture + provider/retrieval metadata
        ↓
Normalized immutable SnapshotEnvelope
        ↓
Append-only SQLite SnapshotStore
        ↓
Time-bounded feature calculations
        ↓
Versioned scoring and data gate
        ↓
Decision ledger / rendered research report
        ↓
Normalized daily JSON + local app / portable HTML archive
```

## Implemented boundaries

- `providers.base` supplies bounded GET-only JSON transport, sanitized response
  headers, retry handling, and injectable offline transports for tests.
- `providers.unusual_whales` is a read-only adapter and `audit` probes endpoint
  availability, schema, timestamps, and rate-limit metadata. Neither can trade
  or create alerts.
- `models.SnapshotEnvelope` stores provider, dataset, symbol, `as_of`,
  `retrieved_at`, raw payload, metadata, and schema version. Both timestamps
  remain explicit and are normalized to UTC for storage.
- `store.SnapshotStore` is local SQLite, deduplicates raw payloads, and uses
  database triggers to prevent update/delete of stored evidence.
- `features` calculates only from observations available at the supplied cutoff.
  It provides trend, flow-quality/OI-confirmation, and volatility features.
- `enhanced_collection` captures compact Greek, volatility, dark-pool-level,
  short, market-tide, and sector-tide evidence with one retry-aware budget
  preflight. Global feeds are collected once per run.
- `enhanced_features` produces a source-linked descriptive summary from the
  latest enhanced snapshots. It does not emit probabilities or recommendations.
- `edge.EdgeAnalyzer` builds the versioned operational research layer: option
  surface, consecutive-chain OI, provisional flow conviction, GEX topology,
  dark-pool price levels, earnings priors, and embargoed historical analogs.
  `option_mechanics` adds a transparent price/time sensitivity matrix.
- `scoring` keeps setup score, directional probability, confidence, evidence
  provenance, and action/data gates separate. Direct observations and derived
  signals have explicit allowed provenance. Missing, stale, mislabeled, or
  contradictory critical evidence blocks a new entry.
- `pipeline` binds normalized inputs to exact snapshot IDs, feature hashes,
  cutoff time, trigger/invalidation assumptions, and a versioned score.
- `ledger.ForecastLedger` appends forecasts and later outcomes. Foreign keys and
  database triggers reject missing lineage, mutation, and look-ahead evidence.
- `dashboard-app` loads prepared, normalized daily JSON. It has no provider
  transport and receives no credential. `artifacts/portable` embeds the same
  normalized view data for offline review. `artifacts/archive` contains frozen
  prior publications that builds must not overwrite.

The `codex-screener` CLI remains offline unless an explicit live command and audit
acknowledgement are supplied. `audit-provider`, `current-capture`,
`enhanced-capture`, `historical-backfill`, and `morning-run` use read-only GET
requests. None can place a trade. `morning-run` keeps recommendations disabled
until freshness, calibration, and execution gates are independently satisfied.

## Time and history policy

The deterministic scheduler defines a 06:45 ET pre-open window and optional
09:40 ET opening-refresh window. It handles weekdays but deliberately does not
claim exchange-holiday handling; an exchange calendar must be validated before
production scheduling.

Use 6–9 months as the normal visible/backfill context for daily and short-term
analysis. Retain an optional two-year sample for out-of-sample validation,
calibration, and regime comparison when licensed history allows. Derived records
must name their raw snapshot IDs, transformation version, scoring version, and
cutoff time in the decision ledger.

## Failure behavior

No input is silently substituted. A missing entitlement, empty response, schema
mismatch, stale source timestamp, incomplete bid/ask, or unconfirmed OI must be
visible to the user and result in a blocked gate or `NO_RECOMMENDATION`, not a
synthetic confidence score.

The derived layer also fails honestly. Historical-state frequencies stay
`DESCRIPTIVE_NOT_CALIBRATED`; risk-neutral option values stay model references;
and attention scores stay review rankings. None can open a decision gate.

A 06:45 ET snapshot can produce a research `WATCH` from accepted prior-session
references. It cannot produce `BUY`. A new entry requires an explicit
execution-ready state, price and spread observations no older than five
minutes, nonempty trigger and invalidation assumptions, and an explicitly
validated calibration state. Until then, even an execution-ready result remains
`WATCH` in shadow mode.
