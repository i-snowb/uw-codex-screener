# Data dictionary

This dictionary separates raw vendor evidence from normalized observations and
derived analysis. A field is not reliable merely because it is present.

| Layer | Field | Meaning and rule |
| --- | --- | --- |
| Raw | provider | Provider identifier; retain exactly with each response. |
| Raw | payload | Unmodified decoded vendor payload, content-hashed in storage. |
| Raw | retrieved_at | Time Codex Screener received the response. Not market time. |
| Normalized | as_of | Time the provider says the observation was true; timezone-aware and no later than `retrieved_at`. |
| Normalized | dataset | Provider-neutral family including base price/chain/flow/OI/GEX/news/event data plus `greek_flow`, `greek_exposure`, `iv_term_structure`, `volatility_stats`, `interpolated_iv`, `dark_pool_levels`, `market_tide`, `sector_tide`, `short_interest`, `borrow`, and `short_volume`. |
| Normalized | symbol | Provider-validated ticker or contract identity. `000660.KS` requires separate exchange/currency validation. |
| Normalized | metadata | Source timestamp semantics, pagination state, data quality, schema version, and entitlement notes. |
| Derived | trend | Signed technical context from available daily bars: returns, EMA, realized volatility, ATR, volume ratio, and optional benchmark-relative return. |
| Derived | flow | Directional premium plus qualifying prints, single-leg share, opening share, and later OI confirmation. Flow direction is an inference, not proof of a buyer's position. |
| Derived | volatility | IV, 20-day realized volatility, IV/RV gap, 90-day IV percentile, term slope, and 25-delta put/call skew. |
| Derived | positioning | Provider-defined GEX levels, call/put walls, gamma flip, and gamma magnet. Preserve method, expiry scope, units, and source time. |
| Derived | option surface | ATM IV by expiry, front/back term slope, 25-delta put-call skew, IV percentile, IV/RV gap, near-money spread, OI, and volume. |
| Derived | OI structure | Per-contract OI difference between consecutive full chains, including near-spot changes and largest build strikes. It does not identify the initiating side. |
| Derived | dark-pool structure | Premium-weighted print-price levels, concentration, price distance, and bid/ask/mid location shares. Owner and direction remain unknown. |
| Derived | Greek topology | Net gamma, vanna, and charm by strike; strongest positive/negative shelves; near-spot regime; and concentration. It is modeled exposure, not verified dealer inventory. |
| Derived | Greek-flow state | Final directional delta/vega flow, OTM share, intraday sign persistence, and late-session change. Provider classification is not proof of opening buyer intent. |
| Derived | volatility pricing | Provider IV/RV spread, IV rank, front/30/60-day term structure, fixed-horizon implied move, and IV percentile. It describes price and dispersion, not direction. |
| Derived | short crowding | Latest short-interest/float, days to cover, borrow fee/availability change, and short-volume ratio versus its 20-day mean. Short volume is not new short interest. |
| Derived | market context | Market and sector net call/put premium and net-volume tide, used only as an alignment or divergence filter. |
| Derived | historical analog | Nearest cutoff-safe price/IV/GEX states within 189 sessions, selected with a 20-session embargo, followed by descriptive 1/5/20-session outcomes. |
| Derived | attention score | Evidence-quality-weighted state extremity used only to order human review. Not probability, expected return, or trade confidence. |
| Derived | option mechanics | Black-Scholes price/time sensitivity using stored ask and IV. Risk-neutral breakeven probability is not physical win probability. |
| Derived | decision fields | Setup score, model directional probability, confidence, action, data-gate status, execution readiness, calibration readiness, reasons, scoring version, and provenance summary. Keep these distinct. |
| Ledger | forecast | Immutable cutoff, generation time, horizon, trigger, invalidation, score/model version, source snapshot IDs, and feature hash. |
| Ledger | outcome | Post-cutoff underlying/option return, maximum adverse excursion, realized volatility, observation time, and friction metadata. |

## Evidence provenance

- `observed`: direct market/provider state, such as price, NBBO spread, or raw OI.
- `inferred`: interpretation from observed context, such as ask/bid-signed flow.
- `modeled`: a calculation, such as trend score, IV percentile, or GEX-derived
  context. Modeled does not mean false; it means the result is not directly
  observable.

The scorer rejects a direct price or spread mislabeled as modeled. It also
rejects derived directional flow mislabeled as a direct observation. This keeps
the UI's `observed`, `inferred`, and `modeled` labels auditable.

## Timestamp and confirmation rules

- Quotes, chains, option trades, flow alerts, dark-pool prints, OI changes, GEX,
  news, and events require their own source time. Do not relabel retrieval time
  as observation time.
- `available_at` records when the pipeline could first use a value. Both market
  time and availability must be no later than the decision cutoff.
- Open interest is generally daily and lagged. A same-day flow inference does not
  become confirmed opening activity until a later OI observation supports it. The
  implemented feature rule confirms an OI ratio of at least 0.5 when qualifying
  trade volume exists.
- GEX is methodology-dependent. It is a positioning estimate, not guaranteed
  support/resistance or a directional forecast.
- A reported dark-pool print does not identify a beneficial owner or thesis.
- News/social fields can identify catalysts or attention; they are not verified
  fundamental facts without source review.

## History policy

Store raw timestamped observations available in the provider trial. The
operational analog model can use up to 504 cutoff-safe daily price sessions,
with a 20-session selection embargo and explicit regime features. Options and
positioning histories use only the provider-accessible window and do not inherit
the longer price coverage. Longer data can support validation only where
coverage, contract adjustments, and survivorship rules are documented.
