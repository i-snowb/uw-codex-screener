# Historical Backfill

Historical backfill preserves raw provider responses and a coverage manifest.
It does not normalize provider fields, score tickers, calculate probability of
profit, or emit a trading recommendation.

## Safety boundary

Run the provider audit first. A response to a request containing `date=` is not
by itself proof that the provider returned data for that market date. The audit
must establish actual scope, timestamps, pagination behavior, and entitlement
limits for the active subscription.

`historical-backfill` is dry-run by default. It makes no network request and
does not write a manifest. A live run needs both `--live` and
`--audit-accepted`. The latter is a human acknowledgement of the documented
audit; it is not a claim that the schema is normalized or complete.

Every run is bounded by `--max-items` (1–2000 logical collection items). Each
item can use at most three HTTP attempts because GET retries are bounded. Before
a live run, the CLI rejects a logical cap whose maximum transport attempts
could cross the protected local request reserve. A later identical run resumes
from the append-only coverage event log. It skips completed raw captures and
retains `scope_unverified` records as intentionally incomplete, rather than
silently turning them into complete history. The legacy `--max-requests` name
remains an alias, but `--max-items` is the accurate term.

## Plan a small first capture

Start with a six-session slice of one ticker. This only displays the exact
planned request count and sample items:

```sh
PYTHONPATH=src python3 -m morning_edge.cli historical-backfill \
  --start-date 2026-08-14 --end-date 2026-08-21 \
  --ticker QCOM --dataset ohlc --dataset option_chain \
  --max-items 20
```

After the live audit is reviewed, the corresponding bounded capture is:

```sh
PYTHONPATH=src python3 -m morning_edge.cli historical-backfill \
  --start-date 2026-08-14 --end-date 2026-08-21 \
  --ticker QCOM --dataset ohlc --dataset option_chain \
  --max-items 20 --live --audit-accepted
```

The output is a coverage manifest with item counts by state. `collected` means
the raw response was stored under the endpoint's verified collection policy; it
does not normalize individual market timestamps. `scope_unverified` explicitly
flags endpoint families whose pagination has not passed the audit.

## Coverage states

| State | Meaning |
| --- | --- |
| `planned` | No collection attempt has been recorded. This includes work deferred by the request budget. |
| `collected` | A raw response was stored and the endpoint has no unresolved pagination condition in this framework. |
| `empty` | A raw response was stored and its top-level `data` was empty. This can be a holiday, entitlement, or no-data condition; inspect the retained payload. |
| `scope_unverified` | A raw response was stored but pagination or historical scope is not established. Do not use it as complete coverage. |
| `failed` | The request or snapshot write failed. The next run retries it. |
| `budget_exhausted` | Reserved for explicit operators that stop a planned item due to quota. Unattempted work remains `planned`. |
| `skipped` | Reserved for an operator-recorded exclusion. |

The weekday planner deliberately does not infer US exchange holidays. Weekday
dates that have no provider data remain explicit `empty` evidence. Raw payloads
are immutable in `snapshots`; manifests and events are also append-only.

## Initial collections

Use `ohlc`, `earnings`, and `option_chain` first. They produce one raw response
per ticker or requested date and are bounded. The active option-chain endpoint
supports a historical `date`, returns the enriched full chain with
`greeks=true`, and exposes no pagination, limit, or cursor parameter. Live
probes confirmed its requested scope at the current date and 30, 60, and 90
days earlier. Accordingly, after adapter date validation, a non-empty
option-chain response is `collected`.

`dark_pool` uses the provider's `older_than` cursor, persists every raw page,
overlaps boundary timestamps, and de-duplicates by `tracking_id`. A short or
empty terminal page is required before a date is complete. `open_interest`
uses numbered pages and fails closed when the provider returns unstable or
duplicate membership; consecutive full-chain snapshots are the preferred
source for canonical per-contract OI deltas.

Dark-pool page timestamps must be non-increasing. Equal one-second timestamps
are valid and are resolved with `tracking_id`; an actual timestamp increase or
an interleaved market-date boundary fails closed as `scope_unverified`.

A dark-pool page may transition once from the requested New York market date
to older dates. When timestamps are strictly descending, that boundary is a
successful terminal condition; older rows remain in the immutable raw page but
do not count toward requested-date coverage. An initial all-older page is
`empty` only when it is short and strictly ordered. Future dates, requested
rows after the older boundary, and non-descending timestamps remain
`scope_unverified`.

`last_tape_time` in an option-chain contract row is a last-trade freshness
signal. It is not an NBBO quote timestamp and must not be used to assert a
current executable bid, ask, or spread.

GEX handling is deliberately three-state. A payload with all four named levels
null is an explicit `empty` observation, not zero GEX. A payload with all four
non-null levels and valid provider date/time can be `collected`. A structurally
present partial level set is retained as immutable raw evidence but ends in
`scope_unverified`; its metadata names the null/blank levels and marks
`derived_gex_eligible: false`. It must not feed a dealer-exposure calculation.
