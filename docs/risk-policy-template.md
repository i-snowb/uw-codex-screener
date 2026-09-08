# Risk-policy template

Complete this policy before any future live recommendation workflow is enabled.
It is a personal risk-control template, not investment advice.

| Policy input | Decision | Value / rule to record |
| --- | --- | --- |
| Allowed symbols | New entries | Default watchlist only, or explicit additions |
| Maximum loss per trade | Position sizing | Dollar and portfolio-percent cap |
| Maximum aggregate options risk | Portfolio | Total premium at risk and correlated-exposure cap |
| Option duration | Entry | Minimum/maximum DTE and permitted expiry windows |
| Liquidity | Entry | Minimum volume/OI and maximum bid/ask spread percentage |
| Earnings/event rule | Entry/hold | Allowed, reduced size, or prohibited; define timing |
| Entry gate | Entry | Required score, confidence, data quality, and observed fields |
| Invalidation | Exit | Technical level, catalyst change, and data-quality failures |
| Profit taking | Management | Trim targets, scale rule, and remaining-risk policy |
| Loss/time stop | Management | Maximum loss, DTE rule, and time in trade |
| Roll rule | Management | When a roll is allowed and maximum added debit |
| Discord/social alerts | Research | Lead-only, evidence weight, retention, and track-record rule |

## Minimum action rules

- Never convert a provider alert, unusual-options label, social message, or
  dark-pool print directly into an order.
- Block a new entry when required observations are missing, stale, modeled rather
  than observed, or directionally contradictory.
- Compare proposed option pricing with executable bid/ask and include spread,
  fees, and worst-case premium loss in the decision record.
- Manage existing positions separately from new-entry scoring. A reduce/exit
  rule may apply even when evidence is stale; document why.
- Record every override with raw snapshot IDs, policy clause, timestamp, and
  reviewer. Do not overwrite earlier decisions.

The implemented scoring module already represents separate setup quality,
directional probability, confidence, and data gate. It does not replace numeric
limits that belong in this policy.

## Implemented draft validation

The daily gateway reads the owner-private `data/private/risk-policy.json`.
`morning_edge.execution_controls.policy_diagnostic` requires these exact fields:

- `policy_id`, `broker_or_quote_feed`, and nonempty `allowed_symbols`;
- positive dollar limits `max_trade_loss_usd`, `max_correlated_risk_usd`, and
  `max_total_options_risk_usd`, in that nondecreasing order;
- positive `max_quote_age_seconds` and `max_spread_fraction` (at most 1);
- positive integer `min_open_interest`, `min_volume`, `min_dte`, and `max_dte`,
  with minimum DTE no greater than maximum DTE;
- `earnings_holding_rule`: `prohibited` or `manual_review`.

Missing, boolean, nonfinite and inconsistent limits fail validation. The owner
must supply these choices. The software does not choose financial limits.
Even valid inputs return `DRAFT_VALIDATED_NOT_APPROVED`, `approved: false`, and
`execution_ready: false`. Setting an `approved` field cannot bypass review.

`quote_diagnostic` also requires the exact `option_symbol`, consistent OSI
identity fields, positive finite `bid` and `ask`, positive integer `bid_size`
and `ask_size`, `open_interest`, `volume`, timezone-aware `quote_timestamp` and
`received_at`, `timestamp_semantics: quote_update`,
`source_kind: executable_nbbo`, and `source` matching the configured feed.
Crossed markets, future or stale quotes, unknown sources, missing fields and
policy violations block the input check. This schema is not a broker connector
and its source labels are not independent attestation. An actual verified feed,
corporate-reference reconciliation, model review and execution policy remain
required. No order routing is implemented or authorized by this diagnostic.
