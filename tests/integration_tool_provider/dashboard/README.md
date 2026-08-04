# Dashboard lens — Playwright probe

Read-only Playwright probe against a live `hermes dashboard` instance,
checking the Capabilities page (app-toolkit catalog + MCP servers section)
against the live NAS toolkit catalog. No toggling of the org's real policy;
only a throwaway stdio MCP stub server is added/removed through the UI.

## What it checks

- (a) Rendered catalog count vs. live NAS count for the same user; the 6
  org-enabled toolkits render distinguishably (no "Disabled" badge) vs. a
  disabled sample.
- (b) Vendor-literal scan across the full rendered DOM, every JSON/text
  network response body, and the browser console — case-insensitive.
- (c) Search, category filter, empty state, clear, no-match query, and a
  combined filter+search.
- (d) Console/page errors and failed/4xx-5xx network requests, on load and
  after interaction.
- (e) Add → persist to `config.yaml` → test connection → remove → confirm
  `config.yaml` cleanup, for a harmless local stdio stub (`/bin/true`,
  no args — touches no network). Also checks whether `/mcp` redirects to
  `/capabilities`.
- (f) Page-load and catalog-fetch timing (via the Navigation/Resource
  Timing APIs, not just wall-clock).

## Prerequisites

- A live NAS (`http://127.0.0.1:3111`) and a dashboard backend running
  against it (`hermes dashboard`) on a free port in 8090-8099.
- A `node_modules` symlink in this directory pointing at an existing
  Playwright + downloaded-chromium install (this repo has none checked in).
  The one used for this probe:

  ```
  ln -sfn /home/daimon/github/tool-gateway/.worktrees/tool-provider-v1/e2e/connect-harness/node_modules node_modules
  ```

  This is a symlink *from* this writable directory *to* a read-only
  reference worktree — it does not write anything into that worktree.

## Standing up an isolated dashboard (never touch real `~/.hermes`)

```bash
# 1. Mint a short-lived (900s) JWT for the canonical seeded user.
TOKEN=$(curl -sX POST http://127.0.0.1:3111/api/internal/dev-mint-oauth-token \
  -H "Authorization: Bearer dummy-auth-secret" -H "Content-Type: application/json" \
  -d '{"userId":"nas_user:f7141b46-b044-41b0-aa13-a36a66f64f26","orgId":"nas_organisation:cfafba9e-77f3-4f72-97e3-dc491fc90c19","clientId":"hermes-agent"}' \
  | jq -r .accessToken)

# 2. Isolated HERMES_HOME. IMPORTANT: don't name it after the vendor — the
#    dashboard's /api/status and /api/profiles echo HERMES_HOME back in
#    their JSON, and that would show up as a false positive in the
#    vendor-literal scan (this bit me on the first attempt).
mkdir -p /tmp/dashprobe/home
grep '^OPENROUTER_API_KEY=' ~/.hermes/.env > /tmp/dashprobe/home/.env   # read-only copy, real ~/.hermes untouched

# 3. auth.json — the dashboard's NAS proxy (hermes_cli/web_routers/capabilities.py)
#    reads this file per-request; no restart needed to pick up a refreshed
#    token (unlike TOOL_GATEWAY_USER_TOKEN, which is baked into env at launch
#    for OTHER code paths). expires_at is ISO-8601, not epoch.
python3 - <<'EOF'
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
token = "$TOKEN"
Path("/tmp/dashprobe/home/auth.json").write_text(json.dumps({
    "providers": {"nous": {
        "access_token": token,
        "portal_base_url": "http://127.0.0.1:3111",
        "client_id": "hermes-agent",
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=870)).isoformat(),
        "scope": "inference:invoke",
    }}
}, indent=2))
EOF

# 4. Launch (note: the CLI subcommand is `dashboard`, not `web`).
cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge
HERMES_HOME=/tmp/dashprobe/home \
HERMES_PORTAL_BASE_URL=http://127.0.0.1:3111 \
TOOLS_GATEWAY_URL=http://tools-gateway.localhost:3009 \
TOOL_GATEWAY_USER_TOKEN="$TOKEN" \
  .venv/bin/python -m hermes_cli.main dashboard --port 8091 --no-open --skip-build --isolated &
```

`--skip-build` serves the already-built `hermes_cli/web_dist` (rebuilt as
needed — check its mtime against `web/src` before trusting it stale-free).
`--isolated` avoids the unified-profile-launch re-exec dance.

## Run

```bash
cd tests/integration_tool_provider/dashboard
BASE_URL=http://127.0.0.1:8091 \
HERMES_HOME=/tmp/dashprobe/home \
NAS_URL=http://127.0.0.1:3111 \
NAS_TOKEN_FILE=/tmp/dashprobe/.token \
  node probe.mjs
```

`NAS_TOKEN_FILE` should contain just the bearer token (no trailing
newline concerns — the script trims it) — never pass the token on the
command line where it could land in shell history or a process listing.

Captured output from the last clean run: `artifacts/probe_run_output.log`.

## Findings from the captured run (see `artifacts/probe_run_output.log`)

1. **`/mcp` does not redirect to `/capabilities`** (high). There is no
   route named `/mcp` anywhere in `web/src/App.tsx`; the catch-all
   `UnknownRouteFallback` sends every unmatched path — including `/mcp` —
   to `/sessions`. A bookmark or doc link to the old MCP page's URL now
   silently lands on Sessions, not the merged Capabilities page.
2. **Vendor literal is genuinely user-visible**, not just latent: NAS's
   own toolkit catalog has one entry (`slug: browser_tool`, `name: Browser
   Tool`) whose `description` field literally starts "Composio Browser
   Tool enables AI Agents...". `CapabilitiesPage`/`CatalogGrid` renders
   `toolkit.description` verbatim, so this string reaches the rendered DOM
   for anyone who scrolls to that card, and it is not covered by the
   catalog-filter exclusion (that filter is by slug, not by scanning
   description text).
3. Same field is present in the `/api/capabilities/toolkits` JSON response
   body: every one of the 100 toolkit entries carries a `logo` URL
   pointing at the vendor's logo CDN host, plus the one description hit
   above (101 total substring hits in that one response body). Unused by
   the frontend today (`BrandGlyph` renders initials, not `toolkit.logo`),
   but it is a live, unredacted network response body the browser
   receives, which is squarely inside this lens's scan surface.

All other checks (catalog count/enabled-distinguishability, filters, empty
state, console/network hygiene, MCP add/persist/test/remove/config.yaml
cleanup) passed clean on the final run — see the log for full evidence.

## Note on an early false-positive (self-inflicted)

The first two runs of this probe (not kept) were launched with
`HERMES_HOME=/tmp/composio-dashboard-test/...` — the scratch dir's own
name contained the vendor string, which the dashboard's `/api/status` and
`/api/profiles` endpoints then echoed back verbatim (they report
`hermes_home` and profile `path`). That produced two bogus "vendor literal
in response body" hits that were purely an artifact of my own directory
naming, not a product defect. Renaming the scratch dir to `/tmp/dashprobe`
and relaunching eliminated both — a reminder that read-only lenses which
echo config paths back to the client need throwaway resource names picked
as carefully as the assertions themselves.
