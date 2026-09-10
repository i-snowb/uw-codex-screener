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

### Offline development experiments and matched cohorts

`scripts/run_research_experiments.py` is a separate read-only consumer of the
snapshot and forecast database. It never initializes a ledger, registers a
forecast, calls providers, publishes a dashboard, or promotes a model.

Run it with a stored raw publication input, the local database, and a new private
output directory. Its default mode audits normalized price inputs and computes
matched prospective forecast comparisons. Historical development additionally
requires both `--run-development` and
`--accept-retrospective-limitations`.

```sh
PYTHONPATH=src:scripts python3 scripts/run_research_experiments.py \
  --input outputs/unattended/YYYY-MM-DD/publication-input.json \
  --database data/morning-edge.sqlite \
  --output outputs/research-experiments/UNIQUE-RUN \
  --benchmark QQQ --run-development --accept-retrospective-limitations
```

The registration artifact pins the input digest, Python source hashes,
benchmark, fixed parameters, hypothesis, endpoints, and holdout policy before
fitting. Outputs are owner-private and refuse replacement with different
contents. Identical reruns are idempotent; use a new directory if source or
inputs change.

The development experiment compares pooled ridge regression with three price
features against the same model plus two benchmark-relative features:

- Price-only: 5-session return, 20-session return, and 20-session realized
  volatility.
- Market-relative addition: stock minus benchmark return at 5 and 20 sessions.
- Targets: absolute stock return and stock-minus-benchmark return, separately at
  1, 5, and 20 sessions. Excess return is not beta-adjusted alpha.
- Controls: capped trend-only return, the training-period mean, and zero return.

Ridge penalty is fixed at 1.0. Training origins receive equal total weight, and
feature scaling is fitted only on that training fold. Expanding folds require
126 training origin dates and test 21 dates at a time. All tickers on one date
stay together. Training labels must end strictly before the first test origin.
Missing sessions invalidate the corresponding feature or target window; the
runner never compresses gaps into shorter horizons.

The last 63 eligible origin dates are reserved. Earlier examples whose labels
reach this boundary are also excluded from development. The holdout is not
scored by this command. This is a logical evaluation boundary, not encrypted
storage: raw prices remain in the private dataset artifact. Do not use those
reserved outcomes to choose features or settings.

Development histories are reconstructed from sources available at the supplied
capture cutoff. Many historical observations were retrieved much later than
their market dates. Consequently these results are explicitly retrospective,
not point-in-time prospective performance. The audit reports internal gaps,
invalid normalized prices, large adjacent moves, current flow coverage, and
headline date/duplicate flags. It checks raw price payload hashes. Adjustment
basis, corporate actions, security lineage, and universe selection remain
unreconciled; no automatic price repair or outlier deletion occurs.

Frozen comparisons select the earliest prospective forecast before checking
whether it has an outcome. They match exact ticker, origin, target, horizon,
cutoff, and feature version. Direction comparisons exclude neutral forecasts
and flat outcomes on both sides, then show paired coverage. Numeric-center
comparisons have their own cohort. Reported baselines are fixed directions or
stored pre-outcome momentum, never the realized majority direction.
The supplied decision-universe count includes missing model predictions.
Candidate/baseline availability, matched coverage of the candidate, and
nonneutral coverage within the matched sample are reported separately. Missing
models are not presented as neutral predictions. Duplicate decision identities
are rejected.

Metrics are paired mean absolute error and directional accuracy, each weighted
equally by origin date. Origin weighting does not eliminate dependence across
dates or overlapping outcomes. No confidence interval, significance, calibrated
probability, executable return, or promotion claim follows from these reports.
No existing V3/V4 formula or ledger record is changed.

The output directory contains `registration.json`, `dataset.json`,
`integrity-audit.json`, `frozen-cohorts.json`, optional
`development-results.json`, and a hash manifest. Provider-derived outputs stay
out of the public source release.

The runner also writes `reconciliation.json` in the same read-only price-input
transaction. It distinguishes absent valid raw candidates, stored candidates
that require scope review, and conflicting candidate closing prices. Each
candidate has source and row hashes; each source reports overlap comparisons,
field revisions, malformed rows, duplicate sessions, and rows after its
requested date. Rows not final at their original capture time are separately
flagged using the existing conservative 16:15 New York completion policy;
retrieving the audit later does not turn a partial bar into a final bar.
Same-provider overlap is not independent validation. Neither
agreement nor a missing legacy flag grants eligibility. The report never
repairs a price, changes a verification flag, or scores a holdout.

Reference inventory is symbol-scoped: a company-identity capture is reported as
captured but unreconciled, not verified historical identity. Missing
corporate-action captures remain explicit. Payload completeness, adjustment
basis, option deliverables, and independent identity verification are outside
this inventory. Price gaps are internal to observed history; expected listing
dates and missing leading or trailing coverage require separate evidence.

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

The v2 challenger suite sorts canonical session dates and rejects duplicate dates,
non-session dates, invalid closes, missing internal NYSE sessions, and bars from
incomplete or future sessions at the supplied cutoff. It returns `INVALID_HISTORY`
with a reason instead of silently treating the next available row as the next
session. Corrected model IDs use `v2`; previously frozen v1 forecasts remain
unchanged. This changes history validation, not the active V3/V4 formulas.

## Offline signal contribution tests

`scripts/run_research_experiments.py --run-development
--accept-retrospective-limitations --run-signal-ablation` adds paired feature-group
tests to the offline experiment. The normal input, database, and output arguments
remain required. The three comparisons are trend added to volatility, volatility
added to trend, and market-relative features added to the price model. Each uses
the same eligible rows within a target and horizon, training-only scaling, and the
fixed ridge penalty. Absolute and QQQ-relative return targets remain separate.

These are exploratory development comparisons on data already inspected. They
are not a fresh confirmatory test, causal attribution, or permission to promote a
model. The sealed holdout stays unscored. Flow and events/news remain explicitly
untested until comparable historical and point-in-time features are validated.

The runner writes a compact `signal-research.json` beside the immutable experiment
artifacts. Publish it separately with:

```bash
PYTHONPATH=src:scripts python3 scripts/publish_signal_research.py \
  --input outputs/EXPERIMENT/signal-research.json \
  --app-root dashboard-app
```

This writes only the research summary and its digest manifest. It does not advance
`latest.json` or write forecast/outcome records. The local app displays the report
below market context, with capture/review timestamps, units, sample counts, and a
return-target selector. It verifies the digest before display and hides current
research during historical replay. Frozen portable publications do not include
this current-research panel.

The shadow option selector evaluates stored references against p10, center, and p90 price scenarios. Its fit score measures contract shape and stored liquidity. It is not chance of profit. Scenario returns use constant stored IV and are not expected returns.

The intraday event ledger supports exact-cutoff records for 30-minute, close, and next-open evaluation. It remains unavailable until at least 60 comparable point-in-time events exist and chronological evaluation passes.

Every live intraday cycle now appends one idempotent event record per ticker to
`data/intraday-events.sqlite`. Its event type is the exact set of refresh tiers.
Features contain the observed price change and deterministic confirmation votes.
Daily model outputs are not copied into the intraday model.

## Signal governance

### Fixed price-only prospective trial

`scripts/freeze_price_trial.py` copies the last development fold's fitted
price-only ridge coefficients for each of 1, 5, and 20 sessions. It does not refit
on the holdout. Features are 5-session return, 20-session return, and annualized
20-session realized volatility. Coefficients, scalers, penalty, ticker universe,
training provenance, activation time, and inference implementation digests are
frozen in the private `data/fixed-price-trial.json`. The file is never included in
the public source release. The command refuses to replace an existing file.

The unattended runner attaches predictions only when creating a new source
artifact from a normal capture or an eligible pre-registration capture recovery.
Existing source checkpoints and validation-only runs are not retrofitted. Cutoffs
before activation cannot generate trial paths. Valid contiguous history must end
on the latest complete session and match the registered origin price. Excluded
tickers retain an explicit reason; corrupt configuration or changed inference
code fails validation instead of silently changing the experiment.

`SHADOW_PRICE_TRIAL` records use the existing append-only registration and exact
target-session outcome evaluator. Active forecasts, ranks, option references,
and calibration gates are unchanged. The fixed trial does not retrain daily.
Its full content digest forms part of its model version.
The analyst evidence packet omits the trial prediction, so the shadow result
cannot influence the active agent's interpretation or research ranking.

Head-to-head reporting pairs only canonical prospective records with the same
ticker, origin session, origin close, publication cutoff, horizon, resolved
target, and realized return. It excludes ambiguous active-model matches and
separates active model versions. Pending and unmatched records are visible but
not scored. Equal-origin mean absolute error and three-class sign accuracy are
reported separately by horizon, with no-change return, always-up direction, and
stored 20-session momentum baselines. A missing momentum baseline makes that
metric unavailable instead of silently scoring a smaller cohort.

The dashboard reads these comparisons from each publication's evaluation
snapshot, including replay. Old publications show an empty state. Sixty matched
origin dates per horizon triggers `REVIEW_REQUIRED`, never automatic promotion.
Overlapping horizons, selection from development results, unverified adjustments,
and lack of cost/fill modeling still require review. No probabilities or ranges
are invented for this point-forecast trial.

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
