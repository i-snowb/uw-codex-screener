# Sunday trial runbook

This runbook prepares the first Monday morning snapshot. It is designed to
collect evidence before any model recommendation is trusted.

## Guardrails

- Treat the dashboard as research, not investment advice.
- Keep the provider key in a local shell environment or uncommitted `.env` file.
  Do not paste it into chat, fixtures, screenshots, logs, or source control.
- A provider subscription and an API plan are often separate products. Confirm
  API entitlement, historical endpoints, rate limits, and market-data delay in
  the provider account before integration.
- Use only the explicit read-only audit path until the endpoint audit below is
  complete. Do not connect audit output to scoring.
- Missing, delayed, crossed, or contradictory inputs must return `NO_RECOMMENDATION`.

## Local preparation

From the project root, the offline-first path needs no package install:

```sh
PYTHONPATH=src python3 -m morning_edge.cli init-db
PYTHONPATH=src python3 -m morning_edge.cli load-fixture fixtures/demo_morning_snapshot.json
PYTHONPATH=src python3 -m morning_edge.cli audit-provider --fixture fixtures/demo_morning_snapshot.json
PYTHONPATH=src python3 -m morning_edge.cli morning-run --fixture fixtures/demo_morning_snapshot.json
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

An editable install is optional if a local packaging toolchain is available:

```sh
python3 -m pip install -e .
codex-screener init-db
```

The CLI does not load `.env` automatically and cannot issue a live
recommendation. Keep any key in a local environment or approved secret manager.
Do not add it to fixtures, logs, or source control:

```sh
set -a
. ./.env
set +a
```

Each command prints structured JSON. The demo fixture is synthetic and can
never produce a buy, sale, or options recommendation.

## First authenticated audit

Run one ticker before the full watchlist. This audit path makes read-only GET
requests and reports `recommendations_enabled: false`:

```sh
PYTHONPATH=src python3 -m morning_edge.cli audit-provider --live --ticker QCOM
```

If the first report is complete and the observed request budget is acceptable,
audit the remaining symbols:

```sh
PYTHONPATH=src python3 -m morning_edge.cli audit-provider --live \
  --ticker QCOM --ticker ARM --ticker AMD
```

Repeat `--ticker` for each symbol in the configured watchlist.

Use `--as-of YYYY-MM-DD` only after confirming each endpoint honors historical
dates consistently. The audit will report `scope_unverified` for current-only
probes instead of presenting them as historical. `--raw-capture
data/uw-audit.jsonl` is optional and can store licensed market data. The capture
is forced to owner-only file permissions; keep it within the provider's
retention terms. Never attach or paste it into chat.

## Provider endpoint audit

Before code is permitted to make an authenticated request, capture the answers
to this checklist for every endpoint used:

1. Exact endpoint and request parameters; authentication location; field names;
   pagination; documented and observed rate limits.
2. Timestamp meaning and timezone. Record whether quotes are real-time,
   delayed, regular-hours only, or include premarket.
3. History available for daily bars, option trades, open interest, and GEX.
   Record survivorship, split adjustment, contract-adjustment, and backfill rules.
4. Option-trade conditions: canceled/corrected flags, sweeps, multileg labels,
   exchange, bid/ask at execution, and whether buy/sell sentiment is inferred.
5. Open-interest timing. Daily OI is generally published after the session; it
   must not be represented as an intraday observation.
6. GEX definition and units. Preserve the provider's methodology and expiry
   scope alongside every result; GEX values cannot be compared across methods
   without normalization.
7. Quote quality. Compare a representative chain and bid/ask with the broker
   before using a calculated option return or probability.
8. Retain raw JSON plus request timestamp and provider metadata. Never retain
   the API key or Authorization header.

The read-only provider adapter is implemented. Connect its sampled responses to
normalization only after they pass this audit. The later collector must save
immutable raw snapshots and the version of every transformation and formula.

## Morning schedule

All scheduled times use `America/New_York`, including daylight-saving changes.

| Time | Job | Required result |
| --- | --- | --- |
| Sunday evening | Connection and permission audit | Proven endpoint list; no keys in logs |
| 06:55 ET | Primary snapshot | Raw price, chain, flow, news, events, OI/GEX metadata saved |
| 07:00 ET | Data-quality gate | Prior-session evidence can rank a `WATCH`; no pre-open `BUY` |
| 09:40 ET optional | Opening refresh | Fresh executable quote gate; separate snapshot, no overwrite |
| After close | Outcome capture | Close, IV, and option quote outcome saved for calibration |

The 06:55 ET time is intentional: it leaves room for early provider updates.
It is not a claim that daily OI or GEX is newly calculated at that minute.

## Data-history and acceptance gates

Use 6–9 months as the visible operating/backfill window. When the provider's
licensing, adjustments, and coverage permit it, retain up to two years as a
separate validation sample. Do not treat the longer sample as an automatic
weighting rule for short-horizon options decisions.

Before connecting a live source, complete every local, Sunday API-trial, and
decision-quality gate in [acceptance-criteria.md](acceptance-criteria.md). The
minimum Sunday evidence is an endpoint-by-endpoint audit for option chain, flow
alerts/trades, OI change, GEX, dark-pool, OHLC, news, and earnings; verified
timestamp semantics; observed rate limits; and a bid/ask comparison with an
independent execution venue. A failed or unknown item remains a visible data
block and must return `NO_RECOMMENDATION`.

## Shadow-mode acceptance criteria

Run the model in shadow mode before relying on its signals. For each forecast,
store the exact inputs, data timestamps, model version, confidence components,
and a `NO_RECOMMENDATION` reason when applicable. Score outcomes at 1, 5, 20,
and 45 sessions, using executable bid/ask assumptions rather than last trade.

Do not promote a score to an actionable recommendation until its calibration,
coverage, and performance are measured out of sample. Separate market-implied
probability, model directional probability, setup quality, and decision
confidence in every output.
