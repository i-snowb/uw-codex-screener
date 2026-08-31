# Unusual Whales trial endpoint audit

This is the trial-time acceptance checklist for the Codex Screener data source. It
does not assume that a web subscription includes REST API entitlement. Record the
plan, trial start time, quota, historical depth, and observed response shape in
the daily audit output before enabling any recommendation score.

## Official references

All route and field claims below come from Unusual Whales' official API reference,
reviewed 2026-08-20:

- [API overview and authentication](https://api.unusualwhales.com/docs) — base URL
  `https://api.unusualwhales.com`, Bearer authentication, and the OpenAPI source
  at `https://api.unusualwhales.com/api/openapi`.
- [Option chains](https://api.unusualwhales.com/docs/operations/PublicApi.TickerController.option_chains)
  — `GET /api/stock/{ticker}/option-chains`; `greeks=true` returns enriched
  contract rows including strike, expiry, type, NBBO, IV, OI, volume, Greeks, and
  `last_tape_time`.
- [Option trades](https://api.unusualwhales.com/docs/operations/PublicApi.OptionTradeController.index)
  and [flow alerts](https://api.unusualwhales.com/docs/operations/PublicApi.OptionTradeController.flow_alerts)
  — underlying trade timestamps (`executed_at`) and aggregated alert timestamps
  (`created_at`). The `unusual=true` alert preset is a provider-defined screen,
  not a proof of opening directional positioning.
- [Ticker OI change](https://api.unusualwhales.com/docs/operations/PublicApi.TickerController.oi_change)
  — `GET /api/stock/{ticker}/oi-change`, page starts at 0, `curr_date` and
  `last_date` identify the compared OI dates.
- [GEX levels](https://api.unusualwhales.com/docs/operations/PublicApi.TickerController.gex_levels)
  — `call_wall`, `put_wall`, `gamma_flip`, and `gamma_magnet`; date defaults to
  the last trading date when omitted.
- [Ticker dark-pool trades](https://api.unusualwhales.com/docs/operations/PublicApi.DarkpoolController.darkpool_ticker)
  — `executed_at` is tape time and `trf_executed_at` is the actual TRF execution
  timestamp, available from 2025-05-01 onward.
- [OHLC](https://api.unusualwhales.com/docs/operations/PublicApi.TickerController.ohlc)
  — candle end time is documented as UTC; max response limit is 2,500.
- [News headlines](https://api.unusualwhales.com/docs/operations/PublicApi.NewsController.headlines)
  — `created_at` is when a headline was created or published; page starts at 0.
- [Historical ticker earnings](https://api.unusualwhales.com/docs/operations/PublicApi.EarningsController.ticker)
  — report date/time, expected move, and historical post-earnings moves.

## Data contract and credential handling

Set `UNUSUAL_WHALES_API_KEY` in the scheduler's secret environment. Do not put
the key in source, a `.env` file committed to git, browser local storage, test
fixtures, audit reports, or chat. `UnusualWhalesClient.from_environment()` is the
only standard construction path for a scheduled run.

The adapter uses only safe HTTP GET requests. It has a timeout, a three-attempt
bounded retry for 429/5xx responses, honors `Retry-After` when present, and reads
common rate-limit headers. It intentionally does not retry authentication errors
or 4xx validation errors. Raw-response capture is opt-in and stores no request
headers; enable it only in a private location with a licensed-data retention rule.

All REST payloads must have the documented `{"data": ...}` envelope. A failed
envelope or minimum shape check is a **schema mismatch**, not an empty signal.

## Sunday trial procedure

1. Create the API trial separately from any dashboard subscription. Verify an API
   key can fetch one known ticker using the endpoint audit, not a browser session.
2. Run `run_trial_audit(client, "QCOM")` and repeat it for each watchlist symbol.
   Preserve the JSON audit report and raw evidence only if retention is approved.
3. Capture the HTTP status, timestamp fields, row count, rate-limit metadata, and
   whether the requested historical date is honored for every dataset.
   A historical `--as-of` audit intentionally reports `scope_unverified` for
   flow alerts, individual option trades, news, and earnings because the current
   probes do not send that date to those endpoints. Do not combine those current
   results with date-bounded evidence.
4. Check five random contracts from the chain against the broker's executable bid,
   ask, expiry, strike, and OI. Do not use `last_price` to value a suggested entry.
5. Check OI on two consecutive completed sessions. `curr_date` and `last_date`
   must represent the dates intended by the dashboard. Treat intra-day OI as a
   state field, not conclusive opening-volume evidence.
6. Compare flow alerts with individual option trades. Flag multi-leg, stock-linked,
   cancelled, and neutral/bid-side prints. A large premium does not establish a
   bullish or bearish directional position.
7. Check dark-pool prints using `trf_executed_at` when present. Keep tape time and
   actual TRF execution time separate; do not sort historical results as though
   they mean the same thing.
8. Confirm GEX's `date` and refresh time. The levels endpoint supplies named
   levels, not enough provenance to infer intraday dealer re-hedging on its own.
9. Measure observed data availability at the planned 06:55–07:05 ET snapshot and
   again after the opening auction. Mark unavailable/stale inputs in the UI and
   default the decision to **No recommendation**.

## Backfill acceptance criteria

The planned research window is six to nine months. Before scoring historical
setups, prove each required endpoint has the needed depth and stable timestamps:

| Dataset | Required for scoring | Trial acceptance check |
| --- | --- | --- |
| Daily and intraday OHLC | trend, ATR, realized-volatility features | Requested 6–9 month timeframes return ordered UTC end times with no unexplained gaps. |
| Enriched option chain | bid/ask spread, IV, Greeks, OI, options payoff | A sampled chain has the documented enriched fields and timestamps; invalid or stale NBBO is excluded. |
| Option flow and alerts | flow context, unusual activity evidence | Individual trade timestamps and alert creation timestamps are both present and not conflated. |
| OI change | opening-interest confirmation | `curr_date`, `last_date`, `curr_oi`, and `last_oi` are present, and next-session availability is observed. |
| GEX levels | support/resistance context | All four named levels are present and tied to an explicit observed market date. |
| Dark pool | off-lit context | Cancellations, tape timestamps, and TRF timestamps are retained; not treated as directional by default. |
| News and earnings | catalyst calendar | Headline published timestamp, source, report date, and report time are present. |

## Recommendation guardrails

The dashboard must preserve the distinction between provider data and inference:

- A flow alert is an observation, not a trade recommendation or a confirmed open.
- GEX levels are context. They are not a directional forecast.
- Report signal freshness as both provider event time and local fetch time.
- Any unavailable, schema-mismatched, or stale critical input produces
  `NO RECOMMENDATION — INPUT QUALITY INSUFFICIENT`.
- Keep a versioned feature set and immutable daily snapshot before evaluating
  future returns. Do not revise an earlier score after its outcome is known.
- Validate any buy/sell threshold out of sample and after bid/ask spread and
  conservative slippage. Display calibration alongside any directional estimate.
