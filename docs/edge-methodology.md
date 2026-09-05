# Derived edge methodology

Codex Screener derives research features from immutable raw snapshots. It does
not convert these features into a trade instruction. Every daily artifact names
the feature version, cutoff, and source snapshot IDs.

## Correctness contract: edge-research-v3

Flow history combines pages from one capture plan and requested session. It
deduplicates alert IDs and excludes rows from adjacent dates. A session is
complete only when its pagination-completion event was available by the cutoff.
The current bounded feed is partial context, not a complete-session sample.
Percentiles and z-scores require comparable complete-session coverage.

An unverified empty response remains missing. A verified empty session can count
as zero only inside the observed price-history window. This avoids treating
pre-listing or unsupported history as neutral activity. Zero flow quality
contributes exactly zero to positioning context.

These corrections change research features. They do not demonstrate predictive
alpha or calibrate probabilities. Reprocessed artifacts have a new identity,
retain the original evidence cutoff, and cannot register prospective forecasts.

## Surface scores

The dashboard keeps six dimensions separate:

| Dimension | Inputs | Interpretation |
| --- | --- | --- |
| Directional edge | Price versus EMA20/50/200, 5/20/63-session returns, watchlist-relative 20-session rank, and 20-session analog median | Strength and direction of price-state evidence. It is not a probability. |
| Long-volatility fit | Front ATM IV percentile and IV minus 20-session realized volatility | Whether stored IV appears cheap or expensive relative to the available sample. Constant-IV option sensitivities do not forecast future IV. |
| Positioning context | GEX regime and migration plus quality-discounted signed flow | Dealer/flow context. It does not establish beneficial-owner intent. |
| Tradeability | Near-money spread, OI, volume, and matched contracts | Whether the stored chain is liquid enough for research. Execution remains blocked without fresh quotes. |
| Catalyst risk | Days to earnings, historical earnings outcomes, and major-news count | Event risk, not directional conviction. |
| Evidence quality | Surface/GEX history and independent-analog sample size | Coverage depth. It is not model confidence or expected return. |

`attention_score` ranks names that have both material state extremes and usable
evidence. It answers “what deserves review first?” It does not answer “what
should I buy?”

`trade_rank` is separate. It orders the watchlist by the current conditional
bullish or bearish thesis score. The score blends directional strength,
evidence quality, positioning alignment, tradeability, and historical-analog
alignment. A disagreement between the technical state and the forecast reduces
the score. Event risk can also reduce it. The score is a relative research
priority from 0 to 100; it is not a win probability, expected return, or order
recommendation.

## Derived evidence

- The volatility surface uses the average IV of the nearest-strike call and put
  for each expiry. The operational history stores front/back IV, term slope,
  25-delta put-call skew, and near-money liquidity.
- Canonical OI change is the per-contract difference between consecutive full
  chains. The bounded provider OI-change page remains a cross-check.
- Flow direction is ask-side minus bid-side premium, signed by call/put type and
  discounted for multi-leg ambiguity. It remains provisional until later OI
  supports opening activity.
- GEX topology tracks provider call wall, put wall, gamma flip, gamma magnet,
  spot distance, and level migration. These are modeled positioning references.
- Dark-pool structure groups print premium into price buckets that are 0.25% of
  spot wide. A dominant price level can support price-response analysis. It
  cannot identify the owner or thesis.
- Earnings priors report the observed implied-move exceed rate, median absolute
  post-event move, and long-straddle outcome medians. Small samples remain
  descriptive.

## Comparable historical states

`nearest-analog-v3` searches at most 504 sessions. The matching vector uses
5-, 20-, and 63-session returns, distance from EMA20 and EMA50, 20-session
realized volatility, and 63-session drawdown. Candidate features use only
fields available on the candidate date. A missing-feature coverage penalty
prevents sparse candidates from appearing artificially close. The nearest
selected dates must be at least 20 sessions apart, which reduces duplicate
regimes and overlapping 20-session outcomes.

Front IV and gamma-flip distance are staged separately. They enter the analog
distance only after at least five non-overlapping candidate states contain both
cutoff-safe derivatives features. Before that gate passes, the dashboard shows
the derivatives state as context but the historical price-state match does not
use it. Flow, OI change, native Greek flow, dark-pool levels, short crowding,
market/sector tide, news, and earnings remain outside the directional match
until their timestamped historical lift is tested.

The dashboard reports 1-, 5-, and 20-session up frequency plus p10, median, and
p90 returns. The status is `DESCRIPTIVE_NOT_CALIBRATED`. These values must not be
shown as win probabilities until a registered walk-forward evaluation passes:

1. Chronological train/test splits.
2. A 20-session overlap embargo.
3. Friction-adjusted option and underlying outcomes.
4. Comparison with simple trend and always-flat baselines.
5. Calibration and stability checks by ticker and regime.

## Experimental forecast path

`analog-path-ensemble-v3` produces a 20-session conditional path only when at
least five embargoed analogs are available. At each session, the path center is
75% of the session-by-session empirical analog median and 25% of a capped
recent-trend prior. The displayed lower and upper paths are the
session-by-session empirical analog p10 and p90 outcomes. They are scenario
bounds, not a confidence interval.

The comparable-state layer also reports the full 20-session empirical path,
maximum favorable and adverse excursions, +5% and −5% first-passage ordering,
10% threshold reach frequencies, median peak/trough timing, and the difference
between matched-state outcomes and a non-overlapping 20-session base rate. The
base-rate comparison reduces overlap but does not remove selection bias or
establish causality. The historical disposition is `BULLISH`, `BEARISH`, or
`MIXED` only when both terminal median and observed up frequency agree; it is a
descriptive label, not a recommendation.

The engine labels every path `EXPERIMENTAL_UNCALIBRATED`. Its directional
frequency is an observed analog fraction and must not be described as a
probability of profit. Forecast accuracy remains unknown until the chronological
evaluation above measures directional accuracy, error, interval coverage,
calibration, stability, and performance after transaction costs against simple
baselines.

## Walk-forward model accountability

`walk-forward-shadow-v2` freezes each published 1-, 5-, 10-, and 20-session
directional thesis in the append-only SQLite forecast ledger. The frozen record
includes its run ID, cutoff, model and feature versions, source snapshot IDs,
origin close, center and range target, trigger, invalidation, and paper option
reference. SQLite triggers reject updates and reject source snapshots that were
not available at the forecast cutoff.

A horizon is scored only after a later stored run contains the required regular
session. The underlying score reports direction correctness, realized return,
center error, empirical-range coverage, favorable/adverse path excursion, and
realized volatility. Direction accuracy is compared with the realized
majority-direction baseline, so a one-sided tape cannot create false model
credit.

The reference option is a paper measurement, not a recommendation or fill. It
uses the published stored ask as entry and the first eligible later stored bid
for the same contract as exit. Expired contracts use intrinsic value. Missing
contracts or bids stay unavailable and are excluded from return aggregates.
Commissions and slippage beyond the observed spread are not yet included.

The dashboard keeps calibration blocked until each horizon has at least 60
direction-evaluable forecasts across at least 20 distinct origin sessions and
the chronological stability, leakage, and friction reviews also pass. Meeting
these count thresholds is necessary, not sufficient, for promotion.

Runs registered after a later-session artifact already exists are labeled
`RETROSPECTIVE_ARTIFACT_SEED`. Their outcomes can test the harness and provide a
diagnostic seed, but they never count toward prospective calibration gates.
Only forecasts labeled `PROSPECTIVE` can enter the promotion sample.

## Option mechanics

Reference contracts include a Black-Scholes price/time matrix using the stored
ask, stored IV, and a fixed 4% risk-free-rate assumption. The displayed
breakeven frequency is risk-neutral under the model. It is not a physical or
real-world probability. The matrix is a sensitivity tool and cannot replace a
fresh executable quote, changing-IV scenario, or position risk limit.

The directional thesis also selects one displayed call or put as a research
reference. Selection favors a usable spread, moderate absolute delta, 30–150
days to expiry, and open interest. It is labeled
`MODEL_SELECTED_REFERENCE_NOT_EXECUTABLE`; it is not an executable option
recommendation and must be re-priced from a fresh chain before use.

## Next provider fields

The current chain-derived implementation avoids extra API calls. The next
trial audit should evaluate native IV term structure, Greek flow, strike/expiry
Greek exposure, net-premium ticks, market/sector tide, dark-pool price levels,
and short interest. These feeds stay disabled until their REST entitlement,
timestamp semantics, retention terms, and quota cost are verified. Streaming
fields also require their documented aggregation rules; partial Kafka messages
must not be treated as running daily totals.
