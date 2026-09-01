# Codex Screener

**Turn an options watchlist into a reproducible daily research queue.**

Codex Screener shows what deserves review, what changed, and which evidence
supports or conflicts with each thesis. Every view is tied to a declared cutoff
and runs locally, so provider credentials and research history stay on your
machine.

> [!IMPORTANT]
> Codex Screener is research software, not investment advice. It does not place
> trades or route orders. Evidence scores are not probabilities. When freshness,
> calibration, or execution checks are incomplete, the system fails closed with
> `NO_RECOMMENDATION`.

<img width="1234" height="604" alt="Codex Screener ranked watchlist with market context and options research references" src="https://github.com/user-attachments/assets/82054d4d-c18a-42c5-8a1b-47a34eb6a1e4" />

## See the day clearly

- **Start with priority.** Rank a defined watchlist by evidence strength and
  research attention.
- **Understand the thesis.** See the direction, strongest supporting evidence,
  counterevidence, confirmation condition, and invalidation condition together.
- **Inspect the source trail.** Keep snapshot IDs, market dates, retrieval times,
  feature versions, and cutoff times attached to the result.
- **Replay what was published.** Review immutable daily snapshots without
  rewriting the forecast after the outcome is known.
- **Keep control local.** Collection runs in Python. The browser receives only
  prepared JSON and never receives the provider key.

The dashboard provides a compact Decision view and a deeper Research view. It
includes ranked theses, daily score changes, freshness status, market context,
price decision levels, interactive price and forecast charts, shadow-model
tracking, and model-accountability results.

## Review one thesis at a time

Each ticker brings price state, options positioning, volatility, flow,
catalysts, and counterevidence into one review surface. Stored option contracts
are research references. They are not executable recommendations.

The chart view keeps the current price path, moving averages, stored positioning
levels, and forecast controls in the same context.

<img width="1219" height="691" alt="Codex Screener daily score change, price decision ladder, and one-year price history" src="https://github.com/user-attachments/assets/e930556f-6067-404f-a8bd-f654afd0e543" />

## Hold the model accountable

Frozen forecasts remain visible after outcomes arrive. The accountability view
separates resolved results from pending rows, compares accuracy with a baseline,
and keeps calibration blocked until the evidence clears the configured gates.

<img width="1233" height="688" alt="Codex Screener model accountability panel with frozen forecast outcomes and blocked calibration" src="https://github.com/user-attachments/assets/d398168a-7df5-487e-8c66-300fb920068f" />

## Try it with safe sample data

You need Python 3.11 or newer. The demo does not need a provider key, make
network requests, or install runtime dependencies.

```sh
git clone https://github.com/i-snowb/uw-codex-screener.git
cd uw-codex-screener

python3 scripts/build_demo_dataset.py \
  --output .codex-screener-private/synthetic-demo-run.json
python3 scripts/build_dashboard_bundle.py \
  --input .codex-screener-private/synthetic-demo-run.json \
  --app-root .codex-screener-private/demo-app \
  --portable-output .codex-screener-private/codex-screener-demo.html
python3 scripts/serve_dashboard.py \
  --root .codex-screener-private/demo-app
```

Open [http://127.0.0.1:8765/](http://127.0.0.1:8765/).

The included server binds to loopback by default. Direct `file://` loading
cannot reliably fetch the dashboard JSON. The portable HTML export can be
opened without the server.

## How it works

```text
Read-only provider evidence
        ↓
Immutable raw snapshots and provenance
        ↓
Cutoff-safe features and shadow models
        ↓
Independent data, calibration, and execution gates
        ↓
Local dashboard and immutable daily replay
```

Codex analysis is optional. The deterministic collector, feature engine,
forecast ledger, evaluator, and dashboard run as local Python workflows. When
Codex is used, its output must cite stored evidence and pass the enrichment
validator before it appears as validated analysis.

## Connect your own data

Live collection requires an Unusual Whales API entitlement. Provider coverage,
retention rights, and request limits depend on your subscription. Review
[DATA_POLICY.md](DATA_POLICY.md) before retaining or sharing provider-derived
data.

Create the owner-private configuration:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .

cp .env.example .env
chmod 600 .env
```

Set these values in the uncommitted `.env` file:

```dotenv
UNUSUAL_WHALES_API_KEY=
CODEX_SCREENER_WATCHLIST=QCOM,ARM,INTC,AMD,NVDA
```

The supplied `.env.example` configures a 06:45 ET snapshot and stores runtime
state under the ignored `.codex-screener-private/` directory. Set
`CODEX_SCREENER_HOME` to an absolute owner-private path if you want the archive
outside the checkout.

Load the environment and run the non-secret readiness check:

```sh
set -a
. ./.env
set +a
python3 scripts/setup_doctor.py
```

The setup doctor reports whether the configuration, database, dashboard paths,
and provider key are available. It never prints the credential.

### Run an explicit read-only collection

Inspect the request plan and provider audit before authorizing live mode. A live
run requires both `--live` and `--audit-accepted`:

```sh
PYTHONPATH=src python3 -m morning_edge.cli morning-run \
  --live --audit-accepted \
  --output outputs/runs/YYYY-MM-DD/morning-run.json \
  --dashboard-output outputs/runs/YYYY-MM-DD/morning-dashboard.html
```

The command uses bounded read-only requests, records request attempts in the
local budget ledger, preserves raw evidence and provenance, and keeps trade
actions disabled. Follow the [daily agent runbook](docs/daily-agent-runbook.md)
for validated enrichment, evaluation updates, and local dashboard publication.

## Safety by design

| Boundary | Behavior |
| --- | --- |
| Network | Offline unless a command explicitly includes `--live --audit-accepted` |
| Credentials | Read by the Python collector; never written to dashboard files |
| Evidence | Bound to source snapshot IDs and a declared cutoff |
| Scores | Research rankings, not calibrated odds or expected returns |
| Options | Stored references until fresh quote, spread, calibration, trigger, and invalidation checks pass |
| Actions | `NO_RECOMMENDATION` whenever a required gate is false |
| Server | Loopback-only by default; no authentication or TLS |

The system does not infer dealer inventory from modeled GEX, beneficial-owner
intent from aggregate flow or dark-pool totals, or short-interest change from
short volume. Missing, stale, contradictory, or unsupported evidence remains
visible and blocks promotion.

## Dashboard controls

- Switch between Decision and Research views.
- Select tickers with the ranking, opportunity map, or alert list.
- Use `j`/`k` or the arrow keys to move through the watchlist.
- Use `f` for Decision view and `r` for Research view.
- Change chart ranges and studies without changing the stored publication.
- Replay an immutable published run from the run selector.
- Send the displayed, stored evidence to a local Codex stress-test prompt.

An optional intraday workflow can update price and persistent Greek-flow
conditions while keeping the daily rank, thesis, V3/V4 paths, and evaluation
origin frozen.

## Operator guides

| Topic | Guide |
| --- | --- |
| Daily collection and enrichment | [Daily agent runbook](docs/daily-agent-runbook.md) |
| Local app and portable archive | [Dashboard guide](docs/local-dashboard-app.md) |
| Selective intraday conditioning | [Intraday refresh](docs/intraday-refresh.md) |
| Provider request reserve | [Request-budget policy](docs/provider-request-budget.md) |
| Historical evidence collection | [Historical backfill](docs/historical-backfill.md) |
| Edge and forecast methods | [Edge methodology](docs/edge-methodology.md) |
| Evaluation and promotion gates | [Research control plane](docs/research-control-plane.md) |
| Components and trust boundaries | [Architecture](docs/architecture.md) |
| Field definitions | [Data dictionary](docs/data-dictionary.md) |
| Personal risk constraints | [Risk-policy template](docs/risk-policy-template.md) |

Scheduled Codex tasks are local application state. Cloning the repository does
not install a schedule or grant provider access. See the
[Codex workflow guide](docs/codex-workflow.md) before creating a recurring task.

## Develop and verify

Run the offline test suite:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Build a source-only public release from the explicit allowlist:

```sh
python3 scripts/build_public_release.py \
  --output /tmp/codex-screener-public
```

The release builder fails if the destination exists, a required allowlisted
source is missing, a private runtime path enters the release, or a non-empty
credential assignment is found. The public release contains source, tests,
documentation, the dashboard shell, and synthetic fixtures. It excludes
provider responses, databases, generated runs, portable live dashboards, and
evaluation history.

## License and data rights

Source code is available under the [MIT License](LICENSE). The license does not
grant rights to redistribute third-party market data. Users remain responsible
for provider entitlement, storage, retention, and redistribution terms.
