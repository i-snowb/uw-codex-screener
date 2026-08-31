# Security policy

## Supported version

Security fixes are applied to the current default branch. Historical generated
dashboards and private data archives are not supported release artifacts.

## Report a vulnerability

Use GitHub private vulnerability reporting for this repository. Do not place a
credential, provider response, private market-data artifact, or exploit detail
in a public issue.

Include the affected revision, entry point, required preconditions, impact, and
the smallest safe reproduction. Remove API keys, account identifiers, positions,
and licensed provider payloads from evidence.

## Deployment boundary

Codex Screener is a local, single-operator research tool. Its HTTP server binds
to loopback by default and has no authentication or TLS. Do not bind it to a
non-loopback interface without adding an authenticated deployment boundary.

Keep `.env`, databases, request ledgers, generated runs, portable dashboards,
and evaluation history private. Rotate a credential immediately if it enters a
terminal log, task transcript, public artifact, or Git history.
