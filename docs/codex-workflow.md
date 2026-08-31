# Codex workflow

Codex Screener can run without language-model analysis. The provider collector,
deterministic features, forecasts, evaluation ledger, and dashboard are ordinary
local Python workflows. Codex adds an optional evidence-bound synthesis layer.

## Start in Codex

Open the repository in Codex and use this request:

> Validate my Codex Screener configuration without printing credentials. Run the
> offline synthetic workflow and tests. Show the bounded provider request plan
> before asking me to authorize a live collection. Use AGENTS.md and the daily
> runbook for every agent-generated analysis.

Do not authorize live collection until the user has reviewed the endpoint plan,
request cost, storage policy, and provider entitlement.

## Daily analysis contract

Agent analysis may use only the immutable run artifact and source snapshot IDs
available at its cutoff. It must identify observations, inferences, conflicts,
and missing evidence. It must not:

- read or print credentials;
- use future data or revise an earlier forecast after its outcome;
- treat GEX as verified dealer inventory;
- treat dark-pool totals or short volume as owner intent;
- label an uncalibrated frequency as a probability;
- authorize or route a trade.

The enrichment validator rejects unsupported field references, stale snapshot
IDs, missing scenarios, and action language. The deterministic action remains
`NO_RECOMMENDATION` unless separate calibrated and execution gates pass.

## Scheduling boundary

Codex scheduled tasks are user-local application state. They are not installed
by cloning this repository and must not contain an API key. A user can create a
local recurring task from `docs/daily-agent-runbook.md` after the offline and
live setup checks pass. Store task outputs under `CODEX_SCREENER_HOME` or another
owner-private path.
