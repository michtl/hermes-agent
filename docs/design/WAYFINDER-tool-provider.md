# Wayfinder — tool-provider bridge + Capabilities dashboard (branch `sid/composio-bridge`)

Prototype branch, never merges to main. Built 2026-08-04, extended 2026-08-05. Pairs with
tool-gateway `sid/tool-provider-v1` and NAS `sid/composio-control-plane` (each has its own
wayfinder). Design spec: `docs/design/tool-provider-bridge.md`. Wire contract: gateway repo,
`docs/tool-provider-v1-contract.md` — **authoritative; when this file and that one disagree, that
one wins.**

**Update 2026-08-05 (integration-hardening session):** the Capabilities page gained per-tool
scoping, a vendor-name scrub on catalog prose, and a working `/mcp` redirect; the proxy gained the
field-name translation that made the scoping UI actually round-trip. Test counts below were
re-derived by running the suites — the previous "617 passed" number could not be reproduced by any
command in this file and has been replaced (see "Verification").

**Update 2026-08-05 (spec-review pass, later the same day):** every count, file path and symbol in
this file was re-checked against the code; all of them held. Two things were *added* rather than
corrected, both cross-repo consequences this file did not carry: the gateway's one still-open
vendor-name leak reaches the **model** through this repo's error passthrough (see "Vendor
neutrality on this side"), and NAS's non-member `500` surfaces here as an opaque `502`.

## Map

- `tools/tool_provider_gateway.py` — typed client for gateway `/v1/*` (origin via
  `resolve_managed_tool_gateway("tools")`, override `TOOLS_GATEWAY_URL`).
- `tools/tool_search.py` — bridge fan-out (local BM25 + gateway search, merged, inline
  schemas), provider-slug dispatch (`SCREAMING_SNAKE_CASE` heuristic), `capture_context_id`.
- `model_tools.py` — provider slugs route through TWO insertion points (`tool_call` gets
  pre-unwrapped in two paths — both patched; regressions hide here). The two are not symmetric:
  the inner `tool_call` arm returns `dispatch_provider_tool_call(...)` directly, while the outer arm
  wraps it in the `_dispatch` closure the surrounding hook/retry machinery drives. So the provider
  path skips the hook seams **at this level**. In the live loop that is masked — `tool_executor`
  unwraps the bridge first and fires the hooks itself — so it is latent, not broken. If you ever
  change how `tool_executor` unwraps, this is where it stops being latent.
- `tools/app_connections_tool.py` — visible connect/status tool; headless-safe (prints URL
  when no display); bounded polling.
- `agent/agent_init.py` + `agent/prompt_builder.py` — session-init `/v1/connections` probe
  (empty list = all enabled) + "External app tools" line in the Nous Subscription block.
- `tools/delegate_tool.py` — children inherit `_tool_provider_context_id`/connected set.
- Dashboard: `web/src/pages/CapabilitiesPage.tsx` (+ `web/src/components/McpServersSection.tsx`,
  `web/src/lib/capability-catalog.ts`), backend proxy `hermes_cli/web_routers/capabilities.py`
  (→ NAS `/api/portal/tools/*`). `/mcp` → `/capabilities` via `McpRedirect` in `web/src/App.tsx`;
  McpPage deleted. (Before 2026-08-05 `/mcp` had no route of its own and fell through
  `UnknownRouteFallback` to `/sessions` — the redirect is what makes the old link land correctly.)
  Design reference under `web/design-reference/` (partial by design — composed HTML +
  Toolkits.jsx + tools.css fully specify the port).
- **Per-tool scoping UI** lives in the Capabilities detail slideover: free-text slugs, one per line
  or comma-separated (`parseToolScope` splits on `[\n,]`, trims, dedupes). Three legible states:
  "All tools allowed" (no override) · "Scoped to N tools" · scoped-to-none, which shows a warning
  and a `window.confirm` before saving because an empty save **blocks every tool in the toolkit**.
- **The three-name seam, and the reason the scoping UI was write-only for a while.** NAS's GET
  returns a per-toolkit `tools`; NAS's PUT returns the whole `toolOverrides` map; this repo uses
  `toolsOverride`. `capabilities.py` translates both directions. The dashboard's own tests passed
  throughout because they stubbed a NAS shape NAS never emits — **a green suite on one side of a
  cross-repo seam proves nothing about the seam.**
- **`tools` absent vs `null` is load-bearing all the way down the wire.**
  `api.setToolkitEnabled(slug, enabled)` sends `{enabled}` only; the proxy forwards `tools` **only**
  when the client explicitly set it (`"tools" in body.model_fields_set`), so NAS sees it as absent
  and *preserves* the saved scope. Clearing a scope must therefore send an explicit `tools: null`
  (`api.setToolkitScope(slug, {enabled, tools: null})`). Sending `[]` instead means deny-all, not
  clear. Omitting the `model_fields_set` check in the proxy would silently turn every toggle into a
  scope wipe — which is exactly the bug that shipped once.
  **The read-back has its own null case, on the same seam.** NAS's `PUT` returns the whole
  `toolOverrides` map, and that map is `null` — not `{}` — once the org has no overrides left
  (NAS persists an empty map as `Prisma.DbNull`). Clearing the *last* scope in an org is therefore
  the one response where `toolOverrides` is not a dict. `capabilities.py` guards it
  (`overrides.get(slug) if isinstance(overrides, dict) else None`) and is correct; verified
  2026-08-05 by reading both sides. **No test covers it on either side of the seam** — which is the
  same shape as the write-only-UI bug, so do not treat the green suites as coverage here.
- **NAS's non-member 500 arrives here as a 502.** `_PASSTHROUGH_ERROR_STATUSES` is
  `{400, 401, 403, 404}`; every other upstream status becomes
  `HTTPException(502, "Nous Portal request failed")`. NAS's portal routes return **500** (not 403)
  when a valid token's user is not a member of the target org — only `org_required` maps to 403
  there; see the NAS wayfinder's gotcha 9. So the Capabilities page's message for "you are not in
  this org" is an opaque gateway error. Worth knowing before debugging a blank Capabilities page as
  a proxy bug.

## Isolated live-run recipe (proven by the e2e field test)

Never touch real `~/.hermes`. Use:
- `HERMES_HOME=<scratch>/hermes-home`
- `HERMES_PORTAL_BASE_URL=http://127.0.0.1:3111` (trusted operator escape hatch in
  `hermes_cli/auth.py` — bypasses host allowlist)
- `TOOLS_GATEWAY_URL=http://tools-gateway.localhost:3009` (glibc resolves `.localhost`)
- `TOOL_GATEWAY_USER_TOKEN=<minted JWT>` AND the same JWT in the isolated
  `auth.json` at `providers.nous.access_token` (+ `portal_base_url`). Entitlement is
  satisfied by local decode of the `paid_access` claim — no network call.
- Model key: copy ONLY the model provider key (e.g. `OPENROUTER_API_KEY`) into the profile.
- Token TTL is 900s and is baked into the process env at launch — RE-MINT AND RELAUNCH,
  a refreshed auth.json does not help a running process.

Mint command lives in the NAS wayfinder. E2E transcripts from the rehearsal are committed at
`tool-gateway/.worktrees/tool-provider-v1/docs/session-notes/e2e-transcripts/` (historical
reference only — regenerate fresh transcripts for any new verification; never rely on
session-scratchpad paths, they die with the session that made them).

## Demo-day notes (from the rehearsal)

1. **Lead with auth-required apps** (Calendar/Gmail/GitHub). Public-API asks (HackerNews)
   invite the model to bypass the bridge with `terminal`+curl — real data, wrong mechanism.
   Composio search also misroutes HN-shaped queries.
2. Mint a fresh token immediately before the demo (900s TTL).
3. Real Google/GitHub OAuth completion is a human step (browser login wall) — the harness
   proves everything up to the wall; `check-connection.ts` (gateway repo) picks up after.
4. **Subagent gateway usage is PROVEN, not "code-complete but unproven"** — corrected 2026-08-05.
   This entry previously listed it as the known open gap, on the basis that the build session's
   child agent fell back to `terminal`+curl. Two live runs on 2026-08-04 closed it: a `delegate_task`
   child issued `tool_call HACKERNEWS_GET_LATEST_POSTS`, the gateway logged the matching billable
   execute, and the child got real data back — with no `terminal`/`curl`/`bash`/`python`/web tool
   anywhere in the child session. Repro + evidence:
   `tests/integration_tool_provider/delegate/` (`run_delegate_probe.sh`, `correlate.py`, artifacts).
   The earlier fallback was a permissions artifact, not an architecture one — `hermes_cli/config_defaults.py`
   documents `delegation.subagent_auto_approve` defaulting to `False` (= auto-DENY).
   Run the probe from a homes-root **outside the repo** (`/tmp/hh-deleg` by default): this
   worktree's directory path contains the vendor name and the agent's CWD is model-visible.
5. Init probe adds ~0.5s to session start; search 2.5–4.6s; execute ~1–1.3s. Independently measured
   at the gateway 2026-08-04: `/v1/search` 2.2–3.5s (provider-dominated, gateway overhead ~15ms),
   single-tool `/v1/execute` 854–1041ms. Small 4–5 tool batches can exceed a 2s envelope.

## Vendor neutrality on this side

The gateway strips the vendor from everything it returns, but the **dashboard talks to NAS
directly**, and NAS's toolkit catalog is the vendor's own live catalog. So this repo needs its own
scrub, and it has a partial one.

**First, the thing this section used to assume and shouldn't: "the gateway strips the vendor from
everything it returns" is not true today, and this repo pipes gateway error prose straight to the
model.** `tools/tool_provider_gateway.py` copies the gateway's `error.message` verbatim into
`ToolProviderGatewayError.message`, and three call sites hand that string to the model unmodified:

| Call site | What the model sees |
|---|---|
| `tools/tool_search.py::dispatch_provider_tool_call` | `tool_error(f"{name}: {exc.message}", code=exc.code)` |
| `tools/tool_search.py` gateway fan-out | `result["gateway_notice"] = f"External app-tool search unavailable this turn: {reason}"` |
| `tools/app_connections_tool.py` | `tool_error(exc.message, code=exc.code)` |

That is fine while every gateway message is vendor-free — which was the point of the gateway's
`callProvider()` fix — **except for one that isn't**: `classifyComposioRoute` still returns
`{"code":"VALIDATION_ERROR","message":"Unsupported Composio route"}` with `expose: true`
(gateway repo, `src/server/providers/composio/classifier.ts`; live-reproduced 2026-08-05, and
recorded as the first entry under that repo's WAYFINDER "Known open edges"). It fires on any
unrecognized path or non-`POST` method — which is exactly the failure mode of a misconfigured
`TOOLS_GATEWAY_URL`, and the gateway wayfinder already documents a path-prefix workaround
(`/api/passthrough/tools/v1/...`) for Vercel previews. A prefix applied twice puts the vendor's name
into a tool result the model reads. **Do not demo against a preview host without checking this
first.** The scrub below covers the dashboard, not the agent loop; there is no scrub on this path at
all.

Now the dashboard side:

- `scrubVendorMentions()` in `web/src/lib/capability-catalog.ts` replaces `/composio/gi` with
  "the tool provider" — and `mergeCapabilityToolkit()` applies it to **`description` only**.
- `toolkit.name` and `toolkit.category` come from the same live catalog, are rendered in several
  places (`CapabilitiesPage.tsx` cards, slideover, admin table), and are **not scrubbed**. NAS's
  catalog filter screens the *slug* (`/^composio/i`), not the display name. Known gap.
- Design intent worth preserving: the function's own doc comment says it is meant to cover "every
  prose field that can originate from the vendor's live catalog". Make that true rather than
  narrowing the comment.

## Verification

Run these; do not quote a remembered number.

- Bridge/agent tests: per-file pytest (repo convention — full-suite runs hit pre-existing
  order-dependent pollution, not this branch's fault). Re-derived 2026-08-05 at commit `13671bbb5`,
  `TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 .venv/bin/python -m pytest <file> -q`:

  | File | Passed |
  |---|---|
  | `tests/tools/test_tool_provider_gateway.py` | 13 |
  | `tests/tools/test_tool_search.py` | 49 |
  | `tests/tools/test_app_connections_tool.py` | 10 |
  | `tests/tools/test_delegate.py` | 63 |
  | `tests/agent/test_agent_init_tool_provider.py` | 2 |
  | `tests/agent/test_prompt_builder.py` | 60 |
  | `tests/hermes_cli/test_web_server_capabilities.py` | 9 |

  Every row above was re-run independently on 2026-08-05 (second derivation, same session day,
  still commit `13671bbb5`) and reproduced exactly. Total across the seven files: **206**.

  **The old "617 passed at commit time" figure is not reproducible** and has been removed rather
  than adjusted: no command in this file produces it (the seven files above total 206; collecting
  `tests/tools tests/agent` finds ~9,300). It was most likely a different selection whose command
  was never written down — which is the lesson: record the command with the count, or the count is
  unfalsifiable.
- Dashboard: `cd web && npm test` → **174 passed / 25 files**, and `npx tsc -b` exits 0. Both
  re-derived 2026-08-05 at commit `13671bbb5` (174 was 156, then 159 in earlier sessions).
  `vite build` is listed here as part of the intended gate but was **not** re-run in the
  spec-review pass — do not quote it as verified.
- The worktree needs its own venv: `uv sync --all-extras` from inside it. `scripts/run_tests.sh`
  swallows its summary when piped — call pytest directly.
- `tests/integration_tool_provider/` holds the live probes (bridge dispatch, connections, delegate
  child, dashboard round-trip incl. the scope-destruction repro with screenshots, full-story). They
  need the live stack and are not part of any pytest default run.
- Live screenshots from the build session: scratchpad `logs/capabilities-{page,admin,slideover}.png`
  — **session-scratchpad paths die with the session that made them.** The durable artifacts are
  under `tests/integration_tool_provider/*/artifacts/`.
