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
