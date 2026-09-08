# Daily agent runbook

This runbook defines the 06:45 America/New_York Codex Screener workflow. It is a
research pipeline. It does not authorize order entry, brokerage access, or a
trade recommendation.

Collection is fail-closed: `collection_failed` and `budget_blocked` return exit
status 2. Inspect the separate `*-failed.json` diagnostic. Do not enrich,
evaluate, or publish that attempt. Existing successful outputs remain intact.
Three consecutive transport failures open the collection circuit; authentication
and quota errors open it immediately. Base failure skips enhanced collection.
Successful base and enhanced captures use one shared end-of-capture cutoff.

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
2. Run `PYTHONPATH=src python3 scripts/run_daily_capture.py --live --audit-accepted`.
   Without these flags it is a network-free plan. The gateway captures five
   benchmark OHLC series, current stock information for each ticker, then both the base and enhanced datasets, and
   writes a source-linked `*-enhanced.json` sidecar. Use standalone
   `enhanced-capture` only for selective refreshes. Store all artifacts under
   `outputs/runs/YYYY-MM-DD/` under the configured private runtime root.
   The default 14-ticker plan is 334 logical requests and at most 1,002 transport
   attempts. Preserve the 7,000 reserve. It refuses an existing output path;
   use a new attempt filename for recovery. It records last-attempt stage and
   diagnostics in `dashboard-app/data/pipeline-status.json`. A
   `BLOCKED` or `FAILED` result must not be published. Success here means
   `AWAITING_VALIDATED_ENRICHMENT`, not finished publication.
3. Audit the normalized artifact before analysis:
   - capture count and dataset status;
   - actual provider market dates for chain, GEX, OI, flow, dark pool, OHLC,
     news, and earnings;
   - Greek-exposure, Greek-flow, IV-term, volatility-stat, interpolated-IV,
     dark-pool-level, market-tide, sector-tide, and latest short-data dates;
   - all recommendation, calibration, and execution gates;
   - current capture snapshot IDs.
4. Process the configured watchlist in bounded batches without delegation.
   Use only fields in this artifact and `provenance.analysis_snapshot_ids`.
   These include cutoff-verified enhanced sources. Use
   `field_source_snapshot_ids` to cite sources associated with each claim's
   actual field, not an unrelated valid ticker snapshot. Each record must:
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
   Context-only revisions with `forecast_registration_allowed: false` or
   `revision_scope: CONTEXT_ONLY_NO_NEW_FORECAST` are skipped by batch evaluation
   and rejected by direct registration. They cannot create additional origins.
7. Build the point-in-time research control record with
   `scripts/build_research_control_plane.py`. Retain versioned units, benchmark
   returns and Greek-flow coverage. Do not treat context features as validated
   predictors or register retrospective recalculations as prospective.
8. Publish the local app with `scripts/build_dashboard_bundle.py --local-only
   --require-ready`. The app must load normalized prepared
   data from `dashboard-app/data/latest.json`; it must not load a provider key
   or call a provider. This writes a dated immutable data file, manifest, replay
   index and on-demand content-hashed ticker detail. Never overwrite a dated
   publication or an existing file under `artifacts/archive/`. Do not build,
   attach or open a portable/inline dashboard unless explicitly requested.
9. Run the targeted tests and static dashboard checks. Confirm:
   - every configured watchlist entry is present;
   - all entries are provenance validated;
   - every action remains `NO_RECOMMENDATION` unless a future, separately
     approved calibrated execution policy is implemented;
   - `node --check dashboard-app/assets/app.js` succeeds; the latest manifest
     hash and every referenced detail hash match their files;
   - market context, watchlist decisions, selected-stock evidence, and model results
     remain in separate labeled sections;
   - evaluation rows retain their original run ID, cutoff, source IDs, model
     version, origin close, direction, target path, and reference option;
   - no evaluation status claims calibration until the minimum sample,
     chronological stability, leakage, and friction gates pass;
   - output files are owner-private.
10. Verify HTTP 200 from `http://127.0.0.1:8765/`. Start
    `python3 scripts/serve_dashboard.py --host 127.0.0.1 --port 8765` if needed.
    Open the verified URL in Chrome. Keep the Mac powered on and Codex running
    for local scheduled execution. Post a compact same-chat update with the run timestamp, actual evidence
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

PYTHONPATH=src python3 scripts/build_research_control_plane.py \
  --input outputs/runs/YYYY-MM-DD/morning-run-enriched.json \
  --output outputs/research-control/YYYY-MM-DD.json

PYTHONPATH=src python3 scripts/build_dashboard_bundle.py \
  --input outputs/runs/YYYY-MM-DD/morning-run-enriched.json \
  --enhanced-input outputs/runs/YYYY-MM-DD/morning-run-enhanced.json \
  --evaluation-input outputs/model-evaluation-summary.json \
  --previous-input outputs/runs/PREVIOUS-SESSION/morning-run-enriched.json \
  --app-root dashboard-app \
  --local-only --require-ready
```

The scheduled workflow publishes to the local browser app only. Build a
portable single-file archive separately, and only when a user requests one.

Use the most recent earlier regular-session artifact for `--previous-input`.
Omit the argument only when no prepared prior run exists. The dashboard then
labels the daily score comparison unavailable instead of reconstructing it.

## Evidence boundaries

Session freshness is `LATEST_EXPECTED_SESSION`, not full-window completeness.
Check base capture results, provider observation dates, pagination coverage,
chain field coverage and model eligibility separately. Bounded latest flow and
dark-pool feeds are context; do not use them for complete-session percentiles.
Short interest and borrow retain explicit reporting dates and remain lagged
context. A recent last trade does not establish bid/ask quote age.

Greek flow sums distinct REST minute buckets. Incomplete, conflicting or
prior-session buckets cannot provide intraday confirmation. An identical
duplicate is deduplicated; an ambiguous revision fails the derivation.
Valid pre/post-market minutes are counted separately and excluded from the
regular-session sum. They do not invalidate a complete 390-minute regular
session. Malformed timestamps, non-session dates, missing regular minutes and
conflicting revisions still block confirmation.

The execution-readiness panel is diagnostic. Research publication success is
not execution approval. It names the still-required independent quote feed,
risk policy, calibration review, event coverage and reference reconciliation.
Do not retry a denied corporate-action endpoint as part of the core morning
capture. Record the HTTP access status and have the account owner review it.

The daily gateway captures `/api/stock/{ticker}/info` with the shared budget and
collection circuit. It validates the returned symbol and binds company identity
and upcoming earnings to immutable, cutoff-verified snapshots. A null earnings
date is unknown, not evidence that no event exists. Provider identity does not
verify corporate actions, option deliverables or adjusted price history.

When the provider macro calendar is empty, the gateway can read
`data/private/reviewed-calendar.json`. This is a reviewed official-source cache,
not an automatic successful feed. Each entry has an event time and source URL;
the document has `reviewed_at` and `expires_at`, no more than 24 hours apart.
Expired, future-dated, malformed or unregistered-source caches fail closed.
Coverage remains partial. Company conferences remain separate from macro
releases and earnings. The September 7 recovery used the official BLS monthly
page because a direct ICS request returned HTTP 403. Refresh the review before
expiry; never extend its timestamp without actually reviewing the sources.

Current raw chain identity checks compare OSI symbol, root, expiry, strike and
option type. They do not verify adjusted deliverables. Selected-contract quote
preflight requires independent quote-update and receipt timestamps, bid/ask
size, an approved source identity and a complete draft policy. Stored last-trade
timestamps, smoothed values and retrieval time cannot substitute for quote age.
The diagnostic never enables execution. See `risk-policy-template.md`.

Recovered dark-pool windows use one capture cohort per ticker/date, every
verified page, tracking-ID deduplication and requested tape-date filtering.
Bounded samples cannot provide complete-session ratios or day-over-day level
changes. Complete provider coverage does not mean all off-exchange activity.

For September 8, 2026 premarket, the expected completed session is September 4.
September 7 is closed. If provider market dates have not advanced, keep the
last successful publication and report the failed readiness gate. Recovery of
September 2–4 evidence must retain actual retrieval times and retrospective
eligibility; it cannot repair the historical prospective record.
