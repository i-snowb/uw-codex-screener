# Intraday refresh

The intraday runner updates the local dashboard during an open NYSE session.
It does not replace the daily model publication.

## Model boundary

- The morning V3 and V4 forecasts remain frozen for evaluation.
- Historical-state selection, rank, thesis, option reference, and validated
  agent analysis remain tied to the morning origin.
- Fresh price state and Greek flow produce a deterministic intraday condition:
  `CONFIRMING`, `MIXED`, `WEAKENING`, or `UNAVAILABLE`.
- Modeled GEX, aggregate dark-pool data, and volatility level remain context.
  They do not receive a directional vote.
- The intraday condition is not a probability, new forecast, recommendation,
  or order.

A future 5-, 30-, or 60-minute predictive model must use a separate version,
feature cutoff, walk-forward test, and evaluation ledger. Do not relabel the
daily model as an intraday model.

## Schedule and request use

The policy uses three independent tiers for the configured watchlist:

- 5 minutes: flow alerts, news, price state, Greek flow, and market, sector,
  QQQ, SMH, and SOXX tides.
- 15 minutes: option chain, named GEX, option activity levels, native
  strike-level Greek exposure, IV term structure, and interpolated IV.
- 30 minutes: dark-pool prints and levels plus volatility diagnostics.

Request cost scales with the watchlist and enabled datasets. Run the dry plan
to get the logical-request count and retry-aware maximum for the current
configuration. Configure the local rolling cap and reserve to fit the provider
plan. These settings are local safety limits, not claims about provider billing.

## Commands

Inspect the plan without network access:

```sh
PYTHONPATH=src python3 scripts/run_intraday_refresh.py \
  --now 2026-08-28T10:00:00-04:00
```

Run one live cycle during an open session:

```sh
PYTHONPATH=src python3 scripts/run_intraday_refresh.py \
  --live --audit-accepted
```

Run until the session closes:

```sh
PYTHONPATH=src python3 scripts/run_intraday_refresh.py \
  --live --audit-accepted --loop
```

The runner reads `.env` without executing it. The file must be owner-private.
The API key stays in Python and is never added to browser data.

```sh
chmod 600 .env
```

## Storage and recovery

- Raw provider responses remain append-only in the configured database under
  `CODEX_SCREENER_HOME` unless an explicit database path overrides it.
- The mutable current run is `outputs/intraday/YYYY-MM-DD/latest-run.json`.
- `outputs/intraday/cycles.jsonl` records each published cycle, its source
  snapshot IDs, request use, and publication digest.
- `dashboard-app/data/latest.json` is replaced atomically. The browser polls it
  every 30 seconds and rerenders only when `dataVersion` changes.
- Dated daily JSON, portable HTML, and frozen archives are not changed.

The runner fails closed when the market is not open, budget is insufficient,
capture fails, the baseline is missing, or the dashboard cannot be published.
