# Codex Screener research control plane

This layer makes model research reproducible. It does not enable recommendations or execution.

## Point-in-time records

`scripts/build_research_control_plane.py` writes one immutable feature record per ticker and a replay manifest. Each record binds:

- effective market session;
- decision cutoff and feature availability time;
- feature and model versions;
- source snapshot IDs;
- missing-field and quality states.

The feature mart rejects a record when `available_at` is after its decision cutoff. Reusing the same record is idempotent. Reusing an identity with different content is an error.

The builder includes derived-history source IDs, checks that every referenced
snapshot exists, and verifies both observation and retrieval times against the
cutoff. Availability is the latest actual source retrieval time. Missing values
receive explicit reasons. `SOURCE_IDS_CUTOFF_VERIFIED` verifies the source set;
it does not prove that each cited source supports every individual claim.
Analyst accountability measures validated `agent_enrichment` when present, not
the older deterministic fallback object.

`research-fields-v2` corrects the GEX flip-distance and long-volatility score
paths, attaches enhanced Greek flow, and expresses all stock/benchmark return
features as fractions. GEX flip distance remains percentage points. It stores
nearest-expiry IV context, Greek coverage and exact-session relative returns.
The version change prevents corrected values from silently changing old mart
records. Daily outputs are compact reusable feature partitions; raw evidence
remains immutable. Reader-local bar caching and SQL source-ID selection reduce
repeated decoding. Capture phase timings are recorded by the daily gateway.

Before adding these fields to a numeric forecast, preregister date-blocked
walk-forward folds, a gap at least as long as the forecast horizon, fixed
market/sector baselines, family ablations and explicit costs. Select features
only on each training fold. Report held-out results by independent origin date,
not correlated ticker rows. Insufficient history must abstain. Corporate-action
adjustment and security-lineage reconciliation remain prerequisites for claims
about large historical jumps; do not remove outliers or manufacture adjusted
prices without source evidence.

The optional `scripts/collect_reference_context.py` stages splits, dividends and
active security listings from the documented Companies API. Its network-free
default plan is 29 logical requests for 14 tickers (87 maximum attempts). Live
capture needs `--live --audit-accepted`, a regular session and the protected
reserve. It stops at the first failure. Do not make this unverified-entitlement
extension a dependency of tomorrow's core capture. Capturing these arrays is
not reconciliation: verify adjustment basis, event dates, prior symbols and
option deliverables before changing price histories or model inputs. The first
authorized capture and reconciliation remain separate acceptance steps.

## Model evaluation

The evaluation ledger tracks the published V3 thesis, V4 shadow forecast, and independent challenger models. Active-thesis reporting requires at least 60 resolved rows and 60 distinct origin sessions per horizon. It reports:

- direction accuracy with a Wilson interval;
- balanced accuracy and Matthews correlation;
- majority, always-bullish, always-bearish, 5-session momentum, and 20-session momentum baselines;
- center error, signed error, interval score, and range coverage;
- results by distinct origin session;
- equal-weight independent-origin accuracy and baseline lift as the primary
  dependence-aware directional metric;
- trend- and volatility-regime slices for instability checks;
- paper option outcomes only when a later stored bid exists.

Analog frequency is not a probability. Probability scoring remains blocked until a calibrated probability forecast exists.

New numeric forecasts use the sign of each horizon's own center return for
direction scoring. The legacy terminal direction remains frozen in old records.
Reports expose a direction-contract breakdown and restrict headline statistics
to the active model version. No historical forecast or outcome is rewritten.

Outcomes require the exact NYSE target session and all intervening session
closes. Missing bars or a conflicting published target date remain pending;
the evaluator never substitutes a later available close. Reprocessed research
cannot be registered as prospective. Full session-path requirements also keep
excursion and realized-volatility measurements comparable.

## Shadow models

The current challenger suite contains:

- ticker-specific L2 logistic direction scores;
- unconditional ticker-specific return quantiles;
- EWMA volatility ranges.

All outputs are shadow-only. Raw logistic scores are not probabilities. A challenger cannot change the ranking, thesis, option row, or recommendation state.

The shadow option selector evaluates stored references against p10, center, and p90 price scenarios. Its fit score measures contract shape and stored liquidity. It is not chance of profit. Scenario returns use constant stored IV and are not expected returns.

The intraday event ledger supports exact-cutoff records for 30-minute, close, and next-open evaluation. It remains unavailable until at least 60 comparable point-in-time events exist and chronological evaluation passes.

Every live intraday cycle now appends one idempotent event record per ticker to
`data/intraday-events.sqlite`. Its event type is the exact set of refresh tiers.
Features contain the observed price change and deterministic confirmation votes.
Daily model outputs are not copied into the intraday model.

## Signal governance

`provider_contracts.py` defines provider field semantics. An unregistered field is context-only. `signal_registry.py` defines the mechanism, horizon, decay rule, falsifier, collection priority, and promotion test for each candidate signal. No signal is validated by default.

## Safe same-day revisions

The first dated dashboard publication remains immutable. A later same-day research revision is stored under a content-addressed path:

`dashboard-app/data/publications/YYYY-MM-DD/<digest>/`

`dashboard-app/data/latest.json` can advance to the new verified revision without changing the original archive.

`dashboard-app/data/publications.json` indexes the immutable revisions used by
the app replay selector. Replay reads the stored normalized artifact; it does not
recompute a forecast with later information.

## Storage and operator checks

`scripts/setup_doctor.py` checks the owner-private environment, key presence,
snapshot database, app shell, and latest prepared data without printing a key.
`scripts/export_historical_partitions.py` writes deterministic monthly gzip
partitions of snapshot metadata and hashes. Raw provider payloads remain in the
SQLite archive and are not copied by the exporter.

## Reproduction

```bash
PYTHONPATH=src python3 scripts/build_research_control_plane.py \
  --input outputs/runs/YYYY-MM-DD/morning-run-enriched.json \
  --feature-database data/research-control.sqlite \
  --evaluation-database data/morning-edge.sqlite \
  --output outputs/research-control/YYYY-MM-DD.json

PYTHONPATH=src python3 -m unittest discover -s tests
node --check dashboard-app/assets/app.js
```

The release must retain `NO_RECOMMENDATION`, `NOT_ELIGIBLE`, and false data, calibration, and execution gates until their explicit tests pass.
