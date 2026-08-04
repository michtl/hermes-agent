# Dispatch lens: `tool_call` routing of gateway (Composio) slugs through `model_tools.py`

Scope: `model_tools.handle_function_call`'s two provider-slug insertion
points, whether gateway dispatch goes through the same guardrail/middleware
surface as any other tool, the SCREAMING_SNAKE routing heuristic, and one
live end-to-end call.

## Files

- `test_tool_call_dispatch.py` — fast pytest probes for (a), (b), (c) below.
- `live_hackernews_probe.py` — the one live call for (d), rerunnable.

## Rerun

```bash
cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge
TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 .venv/bin/python -m pytest \
    tests/integration_tool_provider/dispatch/test_tool_call_dispatch.py -q
```

For the live probe, mint a fresh JWT (900s TTL) and set up an isolated
`HERMES_HOME` (never touch the real `~/.hermes`):

```bash
TOKEN=$(curl -sX POST http://127.0.0.1:3111/api/internal/dev-mint-oauth-token \
  -H "Authorization: Bearer dummy-auth-secret" -H "Content-Type: application/json" \
  -d '{"userId":"nas_user:f7141b46-b044-41b0-aa13-a36a66f64f26","orgId":"nas_organisation:cfafba9e-77f3-4f72-97e3-dc491fc90c19","clientId":"hermes-agent"}' \
  | jq -r .accessToken)

SCRATCH=$(mktemp -d)
mkdir -p "$SCRATCH/hermes-home"
cat > "$SCRATCH/hermes-home/auth.json" <<EOF
{
  "portal_base_url": "http://127.0.0.1:3111",
  "providers": {"nous": {"access_token": "$TOKEN", "expires_at": "2099-01-01T00:00:00Z"}}
}
EOF

export HERMES_HOME="$SCRATCH/hermes-home"
export HERMES_PORTAL_BASE_URL="http://127.0.0.1:3111"
export TOOLS_GATEWAY_URL="http://tools-gateway.localhost:3009"
cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge
.venv/bin/python tests/integration_tool_provider/dispatch/live_hackernews_probe.py
rm -rf "$SCRATCH"   # don't leave a JWT sitting on disk
```

Real HackerNews gateway slugs used in these probes (discovered via
`/v1/schemas` since `/v1/search` misroutes hackernews-shaped queries — known
edge, not a new finding): `HACKERNEWS_GET_USER`, `HACKERNEWS_GET_TOP_STORIES`,
`HACKERNEWS_SEARCH_POSTS`, `HACKERNEWS_GET_LATEST_POSTS`.

## What was found

### (a) Two insertion points — confirmed, both thread args/context_id correctly

`model_tools.handle_function_call` routes a provider (gateway) slug to
`tools.tool_search.dispatch_provider_tool_call` from two places:

1. **The `tool_call` bridge branch** (`model_tools.py` ~L1188-1210): fires
   when `function_name == "tool_call"` and the model wrapped the slug as
   `{"name": <slug>, "arguments": {...}}`. This is inside the tool-search
   bridge-dispatch block, which runs at the very top of
   `handle_function_call`, before `_tool_original_args` is even captured.
2. **The direct-dispatch branch** (`model_tools.py` ~L1361-1367): the
   `elif tool_search.is_provider_tool_name(function_name)` arm of the
   dispatch-function selection, which fires when `function_name` IS the
   slug itself. This runs after tool-request middleware and the
   `pre_tool_call` hook, and its result is wrapped by
   `run_tool_execution_middleware` / `post_tool_call` / `transform_tool_result`
   like any other tool.

`TestBothInsertionPointsThreadArgsAndContextId` in `test_tool_call_dispatch.py`
proves both shapes thread `arguments` and `context_id` unchanged into
`dispatch_provider_tool_call`, and that neither shape ever falls through to
`registry.dispatch` (which would blow up as an unknown local tool).

### (b) Guardrail/middleware parity — NOT symmetric; HIGH finding

The two insertion points are **not equivalent** in what they run through:

- Insertion point 2 (direct slug) goes through the full stack: tool-request
  middleware, the `pre_tool_call` block/approve hook, tool-execution
  middleware, the `post_tool_call` hook, and `transform_tool_result`.
- Insertion point 1 (the `tool_call` bridge) calls
  `dispatch_provider_tool_call` and **returns immediately** — none of those
  seams ever run for the underlying gateway tool.

This matters because gateway slugs are **never** placed directly in the
model-visible tool list. `tools/tool_search.py`'s `assemble_tool_defs` keeps
provider tools out of `filtered_tools` entirely — they only ever surface via
`tool_search`/`tool_describe` and are invoked via the `tool_call` bridge (see
`is_provider_tool_name`'s docstring). So **the only shape the model itself
ever emits for a gateway tool is the `tool_call`-wrapped shape** — insertion
point 1, the ungated one. The `tool_call` schema description shown to the
model (`tools/tool_search.py`, `desc_call`) says in so many words: *"Policy,
hooks, and approvals run exactly as for any directly-listed tool."* For
provider tools reached this way, that promise does not hold at the
`model_tools.py` level.

In the *current* production call chain this is masked, not fixed:
`agent/tool_executor.py` independently pre-unwraps the `tool_call` bridge
itself (in **two** duplicated code blocks — one for the concurrent
execution path, one for the sequential path — both around the comment *"the
unwrap dispatches the underlying tool directly ... bypassing the bridge
branch in handle_function_call and its scope check"*) before ever calling
`handle_function_call`. By the time `handle_function_call` runs,
`function_name` is already the raw slug, so the live agent loop always hits
the correctly-hooked insertion point 2, never insertion point 1.

That means the guard depends entirely on `tool_executor.py`'s own duplicated
unwrap logic staying in perfect sync with `model_tools.py` forever. Any
other caller of the public `handle_function_call` API that doesn't
duplicate that unwrap — the MCP transport
(`agent/transports/hermes_tools_mcp_server.py`, though its current
`EXPOSED_TOOLS` allowlist happens not to include `tool_call`), a future
ACP/batch/RL caller, or anyone calling the documented public API directly —
gets an **unapproved, unaudited external-app-tool execution**: no
block/approve gate, no tool-request rewrite/redaction middleware, no
tool-execution budget/rate-limit middleware, no `post_tool_call` audit
trail, no `transform_tool_result` sanitization. And it is dead code today
only by construction of a second, independent module (`tool_executor.py`)
getting there first — not by any invariant `model_tools.py` itself enforces.

`TestProviderToolCallHookParity` proves this directly with recording
hooks/middleware:
- `test_direct_slug_call_goes_through_the_full_hook_surface` — insertion
  point 2, all 5 seams fire.
- `test_tool_call_bridge_SKIPS_the_hook_surface` — insertion point 1, only
  `provider_dispatch` fires; **verified this test fails (proving it's not
  vacuous) when insertion point 1 is patched to recurse through
  `handle_function_call` the same way the non-provider bridge arm does** —
  confirmed live during this review (temporarily edited `model_tools.py`,
  reran, saw the expected 5-seam trace, then restored the file byte-for-byte
  via diff before finishing).
- `test_bypass_is_specific_to_provider_slugs_not_ordinary_deferred_tools` —
  control: an ordinary MCP/plugin tool routed through the same `tool_call`
  bridge DOES get the full hook treatment (that arm recurses into
  `handle_function_call` with the underlying name instead of dispatching
  directly), isolating the bypass to the provider-tool arm specifically.

**Recommendation**: make insertion point 1 recurse into
`handle_function_call(function_name=underlying_name, ...)` the same way the
non-provider deferred-tool arm already does, instead of calling
`dispatch_provider_tool_call` directly. That's a ~10-line change (see the
diff exercised in the verification above) and removes the reliance on
`tool_executor.py`'s duplicated unwrap for correctness.

### (c) SCREAMING_SNAKE routing heuristic — matrix, no misrouting found

`TestScreamingSnakeRoutingMatrix` documents actual `handle_function_call`
routing (not just the `is_provider_tool_name` predicate in isolation) for:

| name shape | example | `is_provider_tool_name` | actual routing |
|---|---|---|---|
| local tool, SCREAMING_SNAKE, registered | `LOCAL_SCREAMING_TOOL` | `False` (registry entry wins) | `registry.dispatch` — never reaches the gateway |
| unknown SCREAMING_SNAKE, unregistered | `UNKNOWN_SCREAMING_TOOL` | `True` | `dispatch_provider_tool_call` — never reaches `registry.dispatch` |
| lowercase, gateway-slug-shaped | `google_calendar_events_list` | `False` (regex requires SCREAMING_SNAKE) | falls through to `registry.dispatch`, surfaces as an ordinary unknown-tool error — never reaches the gateway with wrong casing |

No misrouting found in either direction. This matches the module's own
documented invariant ("a real local registry entry always wins if one
exists") and existing coverage in
`tests/tools/test_tool_search.py::test_registered_all_caps_tool_wins_over_provider_heuristic`;
this file adds the missing full-dispatch-level check (not just the
predicate) for all three shapes.

### (d) Live end-to-end call

Ran `handle_function_call("tool_call", {"name": "HACKERNEWS_GET_USER",
"arguments": {"username": "pg"}})` against the live gateway + NAS, isolated
`HERMES_HOME`, freshly minted JWT. Real result (JWT redacted, this is the
full unredacted response body — no secrets in it):

```
managed_nous_tools_enabled(): True
gateway config resolved: True
  gateway_origin: http://tools-gateway.localhost:3009
  managed_mode: True
  token present: True len: 1456

--- result ---
{"success": true, "context_id": "dcda99df-bffa-4aa8-a9d2-191c71648f8e", "data": {"about": "Bug fixer.", "karma": 157316, "username": "pg"}}

elapsed_ms=1312.0

OK: live gateway execute round-trip succeeded through handle_function_call('tool_call', ...)
```

Latency: 1312 ms for one `tool_call` -> gateway `/v1/execute` -> HackerNews
API round trip (includes gateway-side Composio dispatch, not just network
RTT to the gateway itself).

This call went through insertion point 1 (the `tool_call` bridge shape,
called directly against the public API rather than through
`tool_executor.py`'s pre-unwrap) — so it also serves as a live demonstration
of the routing path described in finding (b), with no hooks/middleware
registered in this bare interpreter to observe the bypass either way.
