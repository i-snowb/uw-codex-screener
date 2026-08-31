# Data and credential policy

The source-code license does not grant rights to redistribute third-party market
data. Unusual Whales and other provider responses remain subject to their own
terms. Users must verify their entitlement, storage, and redistribution rights.

The public repository contains only source, documentation, and synthetic test
fixtures. These paths are private runtime state and must not be committed:

- `.env` and other credential files;
- `data/` and SQLite sidecars;
- `outputs/` and model-evaluation history;
- `dashboard-app/data/`;
- `artifacts/`, including portable HTML;
- `research/` provider downloads and local audit corpora.

The dashboard browser receives normalized local JSON. It must never receive a
provider credential. A portable HTML export embeds its prepared market data and
is private unless the user has explicit redistribution permission.
