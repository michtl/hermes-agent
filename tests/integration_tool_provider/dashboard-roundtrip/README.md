# Dashboard toggle round-trip — the control-plane money check

**This area is STATE-MUTATING.** It disables and re-enables one toolkit
(`notion`) in the test org's real `OrgToolkitPolicy`, through the real
dashboard UI, and always restores the exact baseline afterwards.

The question it answers: **does a UI toggle actually change what the agent can
do?** Four surfaces are asserted, not one:

| surface | how it is read |
|---|---|
| UI | Playwright against a live `hermes dashboard`, incl. a **full page reload** so a pass cannot come from React-local optimistic state |
| NAS | `GET {NAS}/api/portal/tools/toolkits` |
| DB | the `OrgToolkitPolicy` row in the e2e postgres container (source of truth) |
| Gateway | `POST {GW}/v1/connections` and `POST {GW}/v1/schemas` — what the agent can actually reach |

## One command

```bash
cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge/tests/integration_tool_provider/dashboard-roundtrip
./run.sh
```

`run.sh` captures the baseline **before** anything mutates and registers the
restore on `EXIT INT TERM`, so an interrupt or a probe crash still restores.
Restore is field-by-field, **including array order and `toolOverrides`**, and
it re-reads the row afterwards and shouts loudly (`RESTORE FAILED`, exit 3)
with `CORRECT` vs `CURRENT` if it does not match byte for byte.

Captured output of the run this README documents:
`artifacts/roundtrip_run_output.log`, `artifacts/scope_run_output.log`,
`artifacts/roundtrip_results.json`, `artifacts/scope_results.json`, plus
`artifacts/0*.png` / `1*.png` screenshots.

## Prerequisites

* NAS on `http://127.0.0.1:3111`, gateway on `http://tools-gateway.localhost:3009`,
  the `e2e-postgres-1` container up.
* A `node_modules` symlink to an existing Playwright + chromium install:
  ```
  ln -sfn /home/daimon/github/tool-gateway/.worktrees/tool-provider-v1/e2e/connect-harness/node_modules node_modules
  ```
* A built web dist: `npm run build -w web` (writes the gitignored
  `hermes_cli/web_dist`; the dashboard's own auto-build step fails here because
  `npm install --workspace web` cannot run).
* A dashboard on a free port, launched from an **isolated** `HERMES_HOME`
  (the harness profile), with a known session token so the probe's `fetch`
  asserts can use the same API the browser does:

  ```bash
  # the harness owns the isolated profile + token
  .venv/bin/python tests/integration_tool_provider/harness/hermes_harness.py up     --target local --profile dashrt
  .venv/bin/python tests/integration_tool_provider/harness/hermes_harness.py doctor --profile dashrt

  cat > /tmp/dashrt/launch.sh <<'EOF'
  #!/bin/bash
  cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge
  export HERMES_HOME=$PWD/tests/integration_tool_provider/harness/.homes/dashrt/hermes-home
  export HERMES_PORTAL_BASE_URL=http://127.0.0.1:3111
  export TOOLS_GATEWAY_URL=http://tools-gateway.localhost:3009
  export PYTHONPATH=$PWD
  export HERMES_DASHBOARD_SESSION_TOKEN=dashrt-local-probe-session
  exec .venv/bin/python -m hermes_cli.main dashboard --port 8092 --skip-build --no-open --isolated
  EOF
  chmod +x /tmp/dashrt/launch.sh
  setsid nohup /tmp/dashrt/launch.sh > /tmp/dashrt/dashboard.log 2>&1 < /dev/null &
  ```

  Two launch gotchas, both cost time here:
  * the subcommand is `dashboard`, and it needs `--skip-build --no-open --isolated`;
  * `nohup … &` from a tool-call shell gets reaped when the call returns —
    use `setsid` and a script file, or the server dies the moment you look away.

  `HERMES_DASHBOARD_SESSION_TOKEN` (read by `_resolve_session_token()` in
  `hermes_cli/web_server.py`) pins the dashboard session token; otherwise it is
  a random per-start value injected only into the SPA HTML, and every
  out-of-band `curl`/`fetch` to `/api/*` gets 401.

## The method note that makes the propagation number real

The gateway's policy cache (`src/server/providers/composio/policy.ts`) is an
in-process `Map` keyed by **`principalId`** with a 30 s TTL. So:

* A **fresh token for the same user hits the same warm entry** — reminting
  proves nothing about cold-path latency.
* A cold measurement needs a **different principal**, and that principal must
  be a **real member of the same org**. A non-member gets NAS 403 and the
  gateway fails closed to an empty toolkit set — which is
  *indistinguishable from a real disable* and would silently fake a PASS.

`run.sh` therefore creates a throwaway `User` + `OrgMembership` (role `MEMBER`)
in the test org, mints a token for it, and **validates its policy visibility
against NAS directly** — never through the gateway, which would warm the very
cache being measured. The probe additionally asserts the cold principal still
sees the *other* five toolkits, which is the explicit guard against the
fail-closed-to-empty false positive. The throwaway principal is deleted in the
restore trap.

## Measured results (real run, this machine)

19/19 checks in probe 1, 5/6 in probe 2 (the 1 FAIL is finding #1 below).

```
cold principal  observes the disable  2.1 s after the PUT completes (no cache entry to invalidate)
warm principal  observes the disable  30.0 s after its cache-seeding fetch  (28.5 s after the PUT)
warm principal  observes the recovery 29.4 s after its cache-seeding fetch  (29.3 s after the PUT)
```

Both warm numbers land on the 30 s TTL, measured **from fetch completion**, not
from the change — which is the only way the number is interpretable. The
worst-case staleness a user can experience is one full TTL.

UI, NAS `GET`, and the DB row agreed on every transition, and a full page
reload showed the persisted state in both directions — the UI is not lying.

## Findings

### 1. HIGH — a UI disable → re-enable round-trip silently destroys the toolkit's tool scope

Saved a 2-tool scope for `notion` through the dashboard's "Tool slugs" editor
(persisted: `toolOverrides = {"notion":["NOTION_FETCH_DATA","NOTION_SEARCH_NOTION_PAGE"]}`,
and enforced — see below). Then toggled the toolkit **off and back on** through
the same page, which is exactly what an admin does to cut access briefly.
Result: `toolOverrides` is `null`. The scope is gone, the toolkit is back to
all-tools, and nothing in the UI warns.

Live proof that this widens the agent's reach, from `artifacts/scope_run_output.log`:

```
with the scope active:      NOTION_CREATE_COMMENT -> HTTP 403   (out of scope, refused)
after the off/on round-trip: NOTION_CREATE_COMMENT -> HTTP 200   (schema returned)
```

Cause is on both legs:

* `nous-account-service .../server/composio/policy.ts :: setToolkitEnabled()`
  does `delete toolOverrides[slug]` when `enabled === false` **or** when
  `tools === undefined`;
* `web/src/pages/CapabilitiesPage.tsx :: handleToggle()` calls
  `api.setToolkitEnabled(toolkit.slug, enabled)` with **no** `tools` field.

So the disable clears it and the re-enable cannot bring it back. Either leg
alone would be enough; together there is no path through the UI that preserves
a scope across a toggle.

### 2. LOW — the same round-trip reorders `enabledToolkits`

`["hackernews","github","googlecalendar","gmail","notion","slack"]` becomes
`[…,"slack","notion"]`: `setToolkitEnabled()` rebuilds the array as
`[...existing, slug]`, so a re-enabled slug moves to the tail. The set is
preserved, so nothing functional breaks today, but any consumer that treats
the array as ordered (or any test that compares it literally) will drift, and
it makes "restore to the exact prior state" impossible through the UI alone —
this harness has to reach into the DB to put the order back.

## One correction to the standing known-edges list

The briefing lists "NAS `toolOverrides` are neither persisted by NAS nor
enforced by the gateway" as a known-open defect. Measured today, **both halves
are now fixed**: NAS persists `toolOverrides` to the `OrgToolkitPolicy` row and
echoes it from `GET /api/portal/tools/toolkits`, and the gateway enforces it —
an out-of-scope tool gets `HTTP 403` at `/v1/schemas` while an in-scope tool
gets `200`. A disabled toolkit is likewise refused with
`403 TOOLKIT_NOT_ENABLED`, not merely hidden from `/v1/connections`. That
correction is what makes finding #1 land as HIGH rather than cosmetic: the
scope being destroyed is a scope that was really doing work.

## Files

| file | what |
|---|---|
| `run.sh` | the one command: baseline → cold-principal setup → both probes → guaranteed restore |
| `roundtrip.mjs` | probe 1 — enable/disable round-trip across UI / NAS / DB / gateway, cold + warm propagation timing, reload-honesty |
| `scope_destruction.mjs` | probe 2 — does the round-trip preserve the rest of the policy? plus gateway enforcement of scope and of disable |
| `artifacts/` | captured logs, JSON results, screenshots, and `policy_baseline.json` (the restore target) |
