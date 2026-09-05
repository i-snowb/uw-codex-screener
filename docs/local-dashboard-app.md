# Local dashboard app and portable archive

Codex Screener supports two dashboard formats. Both formats use the same
normalized run data and renderer.

- `dashboard-app/` is the stable local app. It separates HTML, CSS, JavaScript,
  and immutable daily JSON. It is the primary development format.
- `artifacts/portable/` contains self-contained HTML exports. Each file embeds
  its data and code. Use this format for offline review and archival sharing.
- `artifacts/archive/` contains frozen prior publications. Do not rebuild or
  overwrite these files.

## Credential boundary

The local shell displays a wall-clock stale-data warning after six hours and
shows refresh failures without discarding the last successful publication.
This warning is a display rule, not an execution-freshness threshold.
Polling reads the small `data/live-status.json` manifest first. It downloads
`latest.json` only when the content hash changes and verifies that hash before
applying the payload. Replay pauses polling until the user selects Live. An
in-flight refresh cannot replace a newly selected replay.

Base and enhanced captures must share a completed-capture cutoff. The renderer
rejects an enhanced sidecar whose retrieval times exceed the run cutoff.

The browser does not receive the Unusual Whales API key. Collection runs in
Python. It reads the owner-private `.env` and stores licensed provider responses
locally. The app reads only prepared JSON. Do not add credentials to HTML,
JavaScript, JSON artifacts, URLs, commands, or Git history.

To configure a new checkout:

```sh
cp .env.example .env
chmod 600 .env
```

Add the API key to `.env`. The repository ignores `.env`. API access, endpoint
coverage, retention rights, and request limits depend on the user's provider
subscription.

## Build the app and portable export

Run collection and enrichment according to `docs/daily-agent-runbook.md`. Then
build both display formats from the same prepared inputs:

```sh
PYTHONPATH=src python3 scripts/build_dashboard_bundle.py \
  --input outputs/runs/2026-08-28/morning-run-enriched.json \
  --enhanced-input outputs/runs/2026-08-28/morning-run-captured-enhanced.json \
  --evaluation-input outputs/model-evaluation-summary.json \
  --previous-input outputs/runs/2026-08-27/morning-run-enriched.json \
  --app-root dashboard-app \
  --portable-output artifacts/portable/codex-screener-2026-08-28.html
```

The build writes `dashboard-app/data/YYYY-MM-DD/run.json` and a manifest with
SHA-256 digests. It also updates `dashboard-app/data/latest.json`. The dated
file is immutable after publication. The `latest.json` file is a convenience
pointer for the local app.

## Run locally

Direct `file://` loading cannot reliably fetch the JSON data. Use the included
loopback-only server:

```sh
python3 scripts/serve_dashboard.py --root dashboard-app
```

Open `http://127.0.0.1:8765/`. The server does not call a provider and does not
expose the app outside the local computer by default.

During an open session, the separate intraday runner can update
`dashboard-app/data/latest.json` atomically. The browser checks for a new data
version every 30 seconds. Daily publications and portable archives remain
unchanged. See [intraday refresh](intraday-refresh.md).

## What another user gets from GitHub

Another user can clone the project, add their own provider key to an uncommitted
`.env`, run the documented collection workflow, and build the same dashboard
locally. Opening the checked-in HTML alone does not collect new data. Codex also
does not gain provider access from the HTML. The user must configure their own
API entitlement and run the collection command or recurring task.

The portable HTML can open without the local server. It contains the stored
market data for that publication, so confirm that the provider license permits
redistribution before committing or sharing it publicly.
