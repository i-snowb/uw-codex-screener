# Acceptance criteria

These gates must pass before a live provider is connected to the morning
workflow or a dashboard presents an actionable recommendation.

## Local and security gates

- [ ] Offline fixture workflow passes using `PYTHONPATH=src` and Python 3.11+.
- [ ] `python -m unittest discover -s tests -v` passes.
- [ ] API keys are absent from source, fixtures, output, error messages, and raw
  capture headers. A key is read only from a local environment/secret source.
- [ ] Provider calls are read-only, time-bounded, retry-bounded, and have an
  explicit rate-limit response path.
- [ ] Raw snapshots, source times, retrieval times, transformation versions, and
  score versions are retained as append-only evidence.

## Sunday API-trial gates

- [ ] Entitlement and API pricing are confirmed for every endpoint required.
- [ ] One ticker completes endpoint audit for option chain, flow alerts/trades,
  OI change, GEX, dark-pool, OHLC, news, and earnings.
- [ ] The audit output states `recommendations_enabled: false`, and no audit
  result is routed into scoring before field mapping is reviewed.
- [ ] Each response has valid documented fields, source timestamp meaning,
  pagination behavior, and rate-limit observation recorded.
- [ ] Historical coverage supports 6–9 months of operational context; optional
  two-year validation coverage is documented separately.
- [ ] Chain bid/ask and one contract quote agree with an independent execution
  venue within an agreed delay/tolerance.
- [ ] OI date semantics and GEX methodology/expiry scope are visible in output.
- [ ] Empty, delayed, entitlement-denied, and schema-mismatched responses yield
  a visible blocked gate or `NO_RECOMMENDATION`.

## Decision-quality gates

- [ ] The report distinguishes market-implied probability, model directional
  probability, setup score, confidence, and action.
- [ ] New entries require truthful provenance, fresh executable price/spread,
  non-conflicting evidence, explicit trigger/invalidation, and the completed
  risk policy. A 06:55 pre-open run can rank `WATCH` but cannot emit `BUY`.
- [ ] `calibration_ready` remains false until the timestamped out-of-sample
  review passes; shadow-mode estimates cannot emit `BUY` even with live quotes.
- [ ] Current positions receive separate trim/exit/time-risk handling.
- [ ] Daily snapshots are retained and later scored at 1, 5, 20, and 45 sessions
  with executable bid/ask assumptions.
- [ ] The workflow remains in shadow mode until out-of-sample calibration,
  coverage, and failure-rate results are reviewed.

Passing these gates authorizes a controlled connector integration, not trading
automation or a claim that a forecast is reliable.
