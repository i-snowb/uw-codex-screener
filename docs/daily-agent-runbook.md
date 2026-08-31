# Daily agent runbook

This runbook defines the 06:45 America/New_York Codex Screener workflow. It is a
research pipeline. It does not authorize order entry, brokerage access, or a
trade recommendation.

## Preconditions

- Work only in this Codex Screener project.
- Load the owner-private `.env` without printing it.
- Confirm the local provider budget has at least the collector's preflight
  requirement plus the protected reserve.
- Use the current ET date for the run directory.
- Do not reuse an analyst batch from a different run or date.

## Daily sequence

1. Confirm that the day is a regular NYSE session. On a weekend or market
   holiday, do not call the provider. Report `MARKET_CLOSED` instead.
2. Run the explicit bounded live morning command with `--live` and
   `--audit-accepted`. It captures both the base and enhanced datasets and
   writes a source-linked `*-enhanced.json` sidecar. Use standalone
   `enhanced-capture` only for selective refreshes. Store all artifacts under
   `outputs/runs/YYYY-MM-DD/` under the configured private runtime root.
3. Audit the normalized artifact before analysis:
   - capture count and dataset status;
   - actual provider market dates for chain, GEX, OI, flow, dark pool, OHLC,
     news, and earnings;
   - Greek-exposure, Greek-flow, IV-term, volatility-stat, interpolated-IV,
     dark-pool-level, market-tide, sector-tide, and latest short-data dates;
   - all recommendation, calibration, and execution gates;
   - current capture snapshot IDs.
4. Split the configured watchlist into bounded analyst batches. Each analyst can use only
   fields and current snapshot IDs in the base artifact. Each record must:
   - keep `action` equal to `NO_RECOMMENDATION`;
   - distinguish prior-session evidence from current-session evidence;
   - give BULL, BASE, and BEAR conditional scenarios without probabilities;
   - include counterevidence and unknowns;
   - lead with the trade-relevant change, its transmission mechanism, and the
     price or evidence condition that confirms it;
   - rank the two or three strongest decision drivers and state the strongest
     conflict; do not restate every displayed metric;
   - reconcile the 1-, 5-, and 20-session horizons. Include V4 as a shadow
     comparison only; keep the V3 thesis active until the evaluation gate
     promotes another model;
   - keep the summary below 400 characters, each evidence point focused on one
     claim, and each scenario outcome focused on the decision consequence;
   - treat flow, OI, and dark-pool aggregates as non-directional unless a
     separately validated field establishes direction;
   - label displayed option contracts stale and non-actionable unless the
     execution gates are genuinely true.
5. Validate and merge every batch with `scripts/enrich_morning_run.py`. The
   validator rejects missing tickers, duplicate tickers, unknown field paths,
   non-current source IDs, action language, incomplete scenarios, or enabled
   recommendations.
6. Run `scripts/update_model_evaluations.py` against the current date-stamped
   run directory. Do not rescan older artifacts: forecasts already registered
   in the append-only ledger remain available for scoring, and an artifact from
   another database must not enter the active provenance domain. This step
   makes no provider requests. It must register the new
   V3 and V4 1/5/10/20-session forecasts in the append-only SQLite ledger before later
   outcomes exist, score only horizons available in a subsequent stored run,
   and write `outputs/model-evaluation-summary.json`. Missing matching option
   quotes stay unavailable; they must not become zero returns.
7. Rebuild the final self-contained dashboard from the enriched JSON, enhanced
   sidecar, and evaluation summary with
   `scripts/build_enriched_morning_dashboard.py`. Write the date-stamped view to
   an owner-private operator-selected path outside the public repository, then
   display that exact file in the same task.
8. Build the separate local app and portable archive with
   `scripts/build_dashboard_bundle.py`. The app must load normalized prepared
   data from `dashboard-app/data/latest.json`; it must not load a provider key
   or call a provider. Write a dated immutable data file and manifest. Never
   overwrite an existing file under `artifacts/archive/`.
9. Run the targeted tests and static dashboard checks. Confirm:
   - every configured watchlist entry is present;
   - all entries are provenance validated;
   - every action remains `NO_RECOMMENDATION` unless a future, separately
     approved calibrated execution policy is implemented;
   - the portable HTML is below 2 MB, contains no network calls, and its JavaScript
     parses successfully;
   - market context, watchlist decisions, selected-stock evidence, and model results
     remain in separate labeled sections;
   - evaluation rows retain their original run ID, cutoff, source IDs, model
     version, origin close, direction, target path, and reference option;
   - no evaluation status claims calibration until the minimum sample,
     chronological stability, leakage, and friction gates pass;
   - output files are owner-private.
10. Post a compact same-task update with the run timestamp, actual evidence
   dates, quota use, failed or empty datasets, and links to the final JSON and
   dashboard. Never print credentials.

## Failure policy

Stop before analysis if collection fails, quota preflight fails, the run cutoff
is ambiguous, or source IDs cannot be reconstructed. Continue in research-only
mode when a bounded dataset is empty, but display that limitation. Never convert
missing, stale, partial, or contradictory evidence into a zero value, a current
observation, a probability, or an executable option instruction.

## Model-accountability commands

```bash
PYTHONPATH=src python3 scripts/update_model_evaluations.py \
  --database data/morning-edge.sqlite \
  --runs-root outputs/runs/YYYY-MM-DD \
  --output outputs/model-evaluation-summary.json

PYTHONPATH=src python3 scripts/build_enriched_morning_dashboard.py \
  --input outputs/runs/YYYY-MM-DD/morning-run-enriched.json \
  --enhanced-input outputs/runs/YYYY-MM-DD/morning-run-enhanced.json \
  --evaluation-input outputs/model-evaluation-summary.json \
  --previous-input outputs/runs/PREVIOUS-SESSION/morning-run-enriched.json \
  --output outputs/runs/YYYY-MM-DD/morning-dashboard-enriched.html

PYTHONPATH=src python3 scripts/build_dashboard_bundle.py \
  --input outputs/runs/YYYY-MM-DD/morning-run-enriched.json \
  --enhanced-input outputs/runs/YYYY-MM-DD/morning-run-enhanced.json \
  --evaluation-input outputs/model-evaluation-summary.json \
  --previous-input outputs/runs/PREVIOUS-SESSION/morning-run-enriched.json \
  --app-root dashboard-app \
  --portable-output artifacts/portable/codex-screener-YYYY-MM-DD.html
```

Use the most recent earlier regular-session artifact for `--previous-input`.
Omit the argument only when no prepared prior run exists. The dashboard then
labels the daily score comparison unavailable instead of reconstructing it.
