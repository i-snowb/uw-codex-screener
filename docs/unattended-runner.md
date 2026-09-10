# Unattended morning runner

This route does not depend on opening a Codex task. macOS launches a bounded Python
orchestrator. It calls `codex exec` with the explicitly configured `gpt-6-astra`
model and `high` reasoning effort through the owner's existing ChatGPT CLI login.
The analyst receives a bounded evidence projection through standard input. Provider
credentials are loaded only inside the separate capture process, never in the
analyst environment or evidence packet. Approve this evidence transfer to OpenAI
before enabling the route.

## Completion contract

The default window is 06:45–08:00 America/New_York, on NYSE sessions. A five-minute
launchd trigger resumes unfinished work. The runner checks New York time itself,
so a change to the Mac's display timezone does not shift the window. It refuses new
work after the deadline and records `MISSED_DEADLINE`. It does not backdate missed
days or fill gaps with invented prospective predictions.

The workflow is capture → source health → numerical forecast registration →
audited agent batches → enrichment validation → evaluation → research-control
records → archive and latest publication → hash and HTTP readback → `COMPLETE`.
`AWAITING_VALIDATED_ENRICHMENT` is not success. `COMPLETE` means all configured
tickers and available numerical model variants passed the persistence checks.
Research-only actions remain `NO_RECOMMENDATION`; no order route exists here.

Each batch has at most two tickers and two attempts, with a six-minute per-attempt
timeout. Capture attempts are capped at two and twenty minutes each. The morning
deadline is the tighter bound. Existing provider request budgets and reserve gates
still apply. Whole-workflow locks prevent duplicate runners. Atomic immutable
artifacts, durable attempt counters, and content hashes support crash recovery.
Completed batches are not called again. Forecast registration remains idempotent.

A user-authorized late recovery can use a separate private configuration with
`authorized_recovery_date` set to that day's New York date and a deadline no
later than 09:25. It retains actual capture and audit timestamps. The regular
08:00 configuration is not changed. Recovery configuration expires after that
date, and the installer refuses to install it as a recurring job.

If collection completed but local health-stage post-processing failed before any
registration, `--recover-capture outputs/unattended/YYYY-MM-DD/attempts/capture/1`
can rejoin that day's saved evidence using the date-bound recovery configuration.
It rejects collection failures, changed diagnostic logs, other dates, and runs
that already saved source artifacts. It preserves the original cutoff and
numerical forecasts, records the original artifact hashes, and makes no provider
request. It does not admit a calendar or policy reviewed after the cutoff. The
recovery has a separate `YYYY-MM-DD-recovered` directory; the original failed
attempt remains intact. Do not remove either directory to reset retry limits.
The regular runner checks both directories before loading unfinished state. A
completed recovery suppresses another collection only after its saved artifacts,
all ticker audits, and numerical forecast ledger pass readback. A completed
session retains its original code/configuration provenance after later updates;
an unfinished session still refuses a changed code or configuration hash.

## Model and evidence provenance

Private attempt directories preserve the exact prompt, schema, bounded input,
output, runtime events, private stderr, timing, CLI version and binary hash,
requested model, reasoning effort, runtime-reported model when exposed, usage,
source artifact hash, source code hash, and validation result. Audit rows are
append-only in `data/agent-audit.sqlite`. Update/delete triggers reject ordinary
mutation; an administrator can still alter local files or the database. This is
local reproducibility and integrity checking, not an external tamper-proof ledger.

The exact serving revision is unknown unless the runtime attests it. Do not use
model self-identification as evidence. No silent fallback model is configured.
A changed CLI binary blocks execution until it has been revalidated and its pin
explicitly updated. A changed code/configuration hash blocks an in-progress run.
Historical publications remain unchanged. Missing historical model metadata is
shown as unknown in the new dashboard shell.

Agent synthesis and numerical forecasts are separate products. The former is
evidence-bound prose and uncalibrated research ranking. The latter is the existing
V3/V4/challenger forecast ledger. Recording agent provenance does not turn the
language model's prose into a calibrated forecast or a demonstrated trading edge.

## Setup and validation

1. Use absolute operator, Python, and Codex CLI paths. Keep the private config at
   `data/private/unattended-config.json`, mode 0600. Pin the CLI version and SHA-256.
   Keep a private, executable copy of the validated CLI outside the app bundle,
   so app auto-updates cannot replace it. The installed 0.147.0 CLI was rejected
   by the service for Astra; 0.153.0 passed the live two-ticker smoke test.
   A full-watchlist launchd validation is still required by the installer.
2. Confirm `codex login status` reports the intended ChatGPT login. Do not copy
   credentials into a plist. Keep the approved provider configuration private.
3. Run offline tests. Then run a separate frozen-evidence validation with explicit
   data-egress approval:

   ```sh
   python3 scripts/run_unattended_morning.py \
     --config data/private/unattended-config.json --live --audit-accepted \
     --validate-source outputs/runs/YYYY-MM-DD/morning-run.json \
     --validation-directory outputs/unattended-validation/unique-validation-name
   ```

   `--ticker-limit 2` is a smoke test only. Installation requires a full-watchlist
   validation from the current source/configuration and CLI pin. Validation never
   registers prospective forecasts or publishes production data.
4. Run a full validation in launchd's actual environment:

   ```sh
   python3 scripts/install_unattended_agent.py \
     --config data/private/unattended-config.json --launchd-validation \
     --validate-source outputs/runs/YYYY-MM-DD/morning-run.json \
     --validation-directory outputs/unattended-validation/unique-launchd-validation \
     --output outputs/unattended-setup/unique-validation.plist
   ```

   This registers one non-recurring `com.codex-screener.validation` job. Inspect
   its state and logs. The installer requires full validation plus a completed
   launchd job with exit code 0 before enabling the production jobs. Keep this
   validation job registered until installation verifies it; then boot out that
   exact label. A changed source or configuration requires a new validation.
5. Generate and inspect the two production LaunchAgents with `scripts/install_unattended_agent.py`.
   Installation is explicit, requires a successful full validation, and refuses
   to replace an existing plist. The second job owns the loopback-only dashboard
   service independently of Codex. Installation first checks that port 8765 serves
   this operator's current publication. An existing manual server is not killed;
   the managed server retries once per minute until that port is released. Verify
   managed service ownership before retiring the manual server.
   Use `launchctl print gui/UID/com.codex-screener.morning`
   to inspect it. A successful bootstrap alone is not an end-to-end proof.
6. Confirm launchd completion and permissions. The runner does not call the app,
   but also check the first scheduled run with Codex closed. Only after scheduler
   validation should the chat-based execution schedule become a read-only exception monitor.

## Mac prerequisites and residual failure modes

A user LaunchAgent requires a logged-in user. Sleep, shutdown, FileVault's reboot
login screen, network loss, expired authentication, account limits, provider outages,
full disk, and macOS privacy permissions can prevent completion. `caffeinate -is`
prevents idle sleep while the process is running; it cannot start a sleeping or
powered-off computer. It is not a wake schedule. Configure a pre-window wake event
or keep the Mac awake/on AC; changing power settings requires separate approval.
The dashboard server must also run independently for HTTP verification to pass.

This local runner cannot notify you while the Mac is offline. An external dead-man
monitor with a deadline and delivery destination is still required for independent
missed-run alerts. No external service is configured automatically. Dashboard
status and local logs are useful diagnostics, not off-machine monitoring.

## Maintenance and recovery

Check the first actual morning run for `COMPLETE`, 14 validated ticker records,
matching audits and forecast readback, and today's publication cutoff. Review
failure counts and storage weekly; review model/CLI updates before repinning.
Back up the private source artifacts, attempt logs, forecast database, and audit
database to an owner-approved destination. No backup destination is assumed.
Do not delete state to force a rerun: that resets retry limits. Diagnose the saved
failure and use an explicitly labeled validation/recovery run instead.

For a failed batch, inspect its private `audit.json`, `process.json`, and logs.
For missed days, preserve the gap, recover available market data separately, and
label any later analysis retrospective. Never change a past cutoff to create a
successful-looking prospective record. To disable the job, boot out the exact
launchd label and retain the plist and audit files for investigation.
