# Codex Screener

Codex Screener is a local, reproducible research workflow for a user-defined
options watchlist. It is decision support, not investment advice. Network access
is explicit and read-only. The live morning command collects bounded provider
evidence, stores raw responses immutably, builds a cutoff-safe normalized
artifact, and renders a self-contained dashboard.
Validated Codex analysis can then add evidence-bound summaries and conditional
scenarios. No command places trades. Recommendation and option-entry gates fail
closed unless freshness, calibration, and execution requirements are all true.

The daily artifact also contains a versioned derived-edge layer. It separates
direction, long-volatility fit, positioning, tradeability, catalyst risk, and
evidence quality. Drill-downs show why-today deltas, an IV surface, confirmed
consecutive-chain OI changes, GEX migration, dark-pool price levels, earnings
priors, embargoed historical analogs, and an option price/time matrix. See the
[edge methodology](docs/edge-methodology.md).

The included default watchlist is an example. Set `CODEX_SCREENER_WATCHLIST` to
a comma-separated list of supported symbols. Add non-U.S. comparators only after
verifying provider symbol, exchange, currency, options, and market-hours support.

## Public installation

The public repository contains source, tests, documentation, dashboard shell,
and synthetic fixtures. It does not contain provider data or the maintainer's
research history. Each user supplies a private API key and builds a separate
local evidence archive.

```sh
git clone https://github.com/i-snowb/uw-codex-screener.git
cd uw-codex-screener
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set `UNUSUAL_WHALES_API_KEY` and
`CODEX_SCREENER_WATCHLIST`. `CODEX_SCREENER_HOME` defaults to an ignored,
owner-private directory in the checkout; set an absolute path to keep runtime
state elsewhere.
Existing `MORNING_EDGE_*` variables remain backward-compatible; new
configurations should use `CODEX_SCREENER_*`.

Load the owner-controlled environment before a live command:

```sh
set -a
. ./.env
set +a
python3 scripts/setup_doctor.py
```

The setup doctor never prints the credential. Review [DATA_POLICY.md](DATA_POLICY.md)
before retaining or sharing provider-derived data.

## Offline-first quick start

Python 3.11+ and the standard library are sufficient for the checked-in
workflow. From this directory:

```sh
PYTHONPATH=src python3 -m morning_edge.cli init-db
PYTHONPATH=src python3 -m morning_edge.cli load-fixture fixtures/demo_morning_snapshot.json
PYTHONPATH=src python3 -m morning_edge.cli audit-provider --fixture fixtures/demo_morning_snapshot.json
PYTHONPATH=src python3 -m morning_edge.cli morning-run --fixture fixtures/demo_morning_snapshot.json
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The fixture is synthetic. It is used only to test the offline and fail-closed
paths.

Build a synthetic UI dataset without provider access:

```sh
python3 scripts/build_demo_dataset.py
python3 scripts/build_dashboard_bundle.py \
  --input examples/synthetic-demo-run.json \
  --app-root /tmp/codex-screener-demo \
  --portable-output /tmp/codex-screener-demo.html
python3 scripts/serve_dashboard.py --root /tmp/codex-screener-demo
```

## Dashboard formats

The generated dashboard has two supported formats. `dashboard-app/` is the
stable local architecture. It separates code from immutable daily JSON and is
the primary format for development. `artifacts/portable/` contains the same
dashboard as a self-contained HTML file for offline review. Frozen prior files
remain in `artifacts/archive/` and are not overwritten.

The browser never receives the provider key. Another user can clone the project,
copy `.env.example` to an uncommitted `.env`, add their own key and watchlist,
run collection, and build the dashboard. Opening an HTML file does not collect
new data. See the [local dashboard guide](docs/local-dashboard-app.md).

Before a live run, use the non-secret readiness check. For a public UI preview,
build the deterministic synthetic dataset. Neither command contacts a provider:

```sh
python3 scripts/setup_doctor.py
python3 scripts/build_demo_dataset.py
```

Codex agent enrichment is optional. A provider key enables collection and the
deterministic engine; it does not install a scheduled Codex task. See the
[Codex workflow guide](docs/codex-workflow.md) for the evidence-bound analysis
prompt and local scheduling boundary.

The hosted app includes Focus and Research views, URL-stable ticker selection,
keyboard ticker navigation, a mobile ticker bar, and an as-published replay
selector. Each immutable publication is indexed in
`dashboard-app/data/publications.json`. Focus view leads with the current
decision state. Research view exposes model, provenance, and signal detail.

## Intraday conditioning

The local app can refresh selected provider evidence during an open NYSE
session. The daily rank, thesis, V3/V4 paths, agent analysis, and evaluation
origin stay frozen. Fresh price and persistent Greek-flow evidence update a
separate `CONFIRMING`, `MIXED`, `WEAKENING`, or `UNAVAILABLE` condition. This
prevents five-minute observations from rewriting the result that the evaluation
ledger is measuring. See the [intraday refresh guide](docs/intraday-refresh.md).

## Live morning run

Keep provider credentials in the owner-private `.env` file. Do not put them in
commands, reports, or dashboard files. The bounded live collector is explicit:

```sh
set -a
. ./.env
set +a
PYTHONPATH=src python3 -m morning_edge.cli morning-run --live --audit-accepted \
  --output outputs/runs/2026-08-24/morning-run.json \
  --dashboard-output outputs/runs/2026-08-24/morning-dashboard.html
```

This command produces deterministic shadow analysis and automatically captures
the enhanced Greeks, volatility, dark-pool-level, short, and market-context
bundle. It writes a source-linked `*-enhanced.json` sidecar next to the base
artifact. The recurring Codex task then performs the evidence-only analyst pass,
validates every cited field path and source snapshot, merges the result, and
rebuilds the dashboard. See the [daily agent runbook](docs/daily-agent-runbook.md).

The enhanced collector can also run independently with the same owner-private
environment and request ledger:

```sh
PYTHONPATH=src python3 -m morning_edge.cli enhanced-capture --live --audit-accepted
python3 scripts/build_enhanced_summary.py \
  --output outputs/runs/2026-08-24/enhanced-summary.json
```

Run `enhanced-capture` without `--live` to inspect its exact logical-item and
worst-case retry cost. Market and sector tides are collected once per run, not
once per ticker. Short-interest, borrow, and short-volume datasets can be
selected for a lower-frequency weekly refresh.

## Historical backfill

The historical collector is an append-only, raw-response framework. It is
dry-run by default and needs an explicit `--live --audit-accepted` after the
provider audit. It has a hard logical-item cap plus a retry-aware transport
budget and creates a resumable coverage manifest; it does not normalize live
fields or enable recommendations. See the
[historical backfill guide](docs/historical-backfill.md).

## Provider request budget

Live Unusual Whales requests use a shared local trailing-seven-day budget:
30,000 total attempts with 20,000 protected reserve, leaving 10,000 ordinary
attempts before the client fails closed. Retries and failed connections count
conservatively. This is a deliberately strict local policy, not a claim about
the provider's reset schedule. See the
[provider request budget guide](docs/provider-request-budget.md).

## Sunday API audit

After the API trial is active, keep the key in the local environment. Start
with one ticker. The command performs nine authenticated GET probes and leaves
recommendations disabled:

```sh
set -a
. ./.env
set +a
PYTHONPATH=src python3 -m morning_edge.cli audit-provider --live --ticker QCOM
```

Repeat `--ticker` to audit more names. Add `--raw-capture data/audit.jsonl` only
if local retention of licensed response data is permitted. The API key is never
written to the report or capture. See the runbook before using either option.

## Current boundary

- Live fields are normalized from the audited response shapes, but provider
  timestamps control freshness. A Monday retrieval of Friday data remains
  labeled Friday data.
- Research-priority and evidence-confidence scores are shadow research aids.
  They are not calibrated odds, expected returns, or trade confidence.
- The 06:45 ET run ranks research attention, but cannot emit `BUY`, `SELL`, or
  an eligible option entry while any required gate is false.
- Option contracts are reference rows until a fresh executable quote, current
  spread, calibrated thesis, explicit trigger, and invalidation all pass.
- Complete the personal limits in the risk-policy template before enabling any
  position-size or contract recommendation.
- Discord alerts remain hypotheses unless the server authorizes read-only bot
  access and a complete timestamped alert history is evaluated.

## Build a public release

Do not publish this populated working directory. Build a source-only release
from the explicit allowlist:

```sh
python3 scripts/build_public_release.py --output /tmp/codex-screener-public
```

The command fails if the destination exists, a required source file is missing,
a private runtime path enters the release, or a non-empty provider credential
assignment is found. Initialize Git or build a GitHub release from the generated
directory only after reviewing `release-manifest.json`.

If an editable local install is convenient, it is optional and may require a
local packaging toolchain:

```sh
python3 -m pip install -e .
codex-screener init-db
```

## Schedule and data scope

All scheduled windows are `America/New_York`:

| Window | Purpose | Status |
| --- | --- | --- |
| 06:45 ET | Primary pre-open snapshot, bounded analysis, and dashboard | Recurring Codex task |
| 09:40 ET | Optional opening quote and options-flow refresh | Not configured |

Use six to nine months of visible/backfill history for the near-term dashboard
context. Keep up to two years as an optional validation sample when the source
permits it; do not let older regimes dominate short-horizon option decisions.

See [architecture](docs/architecture.md), the [data dictionary](docs/data-dictionary.md),
the [risk-policy template](docs/risk-policy-template.md), and the
[Sunday trial runbook](docs/sunday-trial-runbook.md) before connecting a trial.
