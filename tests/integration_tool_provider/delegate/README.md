# delegate → gateway: does a CHILD agent call bridge tools?

**Status: PROVEN. A `delegate_task` child calls the bridge and gets real data.**
The previously-open gap ("code-complete, never proven live; the build session's
child fell back to terminal+curl") is closed. Two live runs, 2026-08-04.

## One command

```bash
bash /home/daimon/github/hermes-agent/.worktrees/composio-bridge/tests/integration_tool_provider/delegate/run_delegate_probe.sh
```

It stands up an isolated profile via the shared harness (`up` + `doctor`), drives
the parent, then runs `correlate.py` to join the hermes session store against the
gateway dev log. Knobs: `PROFILE`, `HOMES`, `SCENARIO`, `GWLOG` (see the script header).

Homes-root defaults to `/tmp/hh-deleg`, **outside the repo on purpose** — the
worktree directory name contains the upstream vendor name and the agent's CWD is
model-visible.

## The proof (run 2, profile `deleg2`, scenario `delegate_bridge_child_slug.txt`)

Parent session `20260804_194323_f234f2` → child session `20260804_194333_c1b539`
(`sessions.parent_session_id` links them).

| when (UTC) | session | what |
|---|---|---|
| 19:43:32.573 | parent | `delegate_task` tool call — parent does no bridge work itself |
| 19:43:38.121 | **child** | `tool_call {"name":"HACKERNEWS_GET_LATEST_POSTS","arguments":{"size":5}}` |
| 19:43:38.162 | gateway | `Composio billable execute request` requestId `1e2aa8cb-…` `toolCount:1` |
| 19:43:39.142 | gateway | `… execute response` `providerMs:916.31` `resultCount:1` |
| 19:43:39.145 | **child** | tool result `{"success": true, "context_id":"1e2aa8cb-…", "data":{"hits":[…]}}` |
| 19:43:48.499 | parent | `delegate_task` result carrying the child's 5 real HN titles |

No `terminal`, `curl`, `bash`, `python`, or web tool appears in the child session.
Real titles came back (e.g. `"Web security is too hard"`) — see
`artifacts/run2_transcript.txt`.

**Child gateway-call latency:** 1024 ms client-observed (child assistant emit →
child tool result), 980 ms gateway wall, `providerMs 916.31`. A direct `curl` of the
same slug from this box measured 1055 ms, so the bridge adds no meaningful overhead.

## Reproduced by the script itself (run 3)

`run_delegate_probe.sh` was executed end to end, `exit=0`, `elapsed=40.4s`. New parent
`20260804_194605_88299d` → child `20260804_194614_0d0570`; child `tool_call` at
19:46:22.193 → result 19:46:23.390 (**1197 ms** client-observed), gateway execute
requestId `545a7ed5-4cb1-41ab-a2e3-0f988db2652e` at 19:46:22.228 — again byte-identical
to the `context_id` the child received. 5 real titles, `CHILD_BRIDGE_OK 5`,
`PARENT_RELAY_OK`. See `artifacts/run3_scripted_transcript.txt` and
`artifacts/run3_scripted_correlation.txt`.

Also visible in run 3's gateway trace: **each delegated child fires its own session-init
`/v1/connections` probe** (19:46:05 parent, 19:46:14 child, both `toolkitCount:6`
`resolvedFromEmptyRequest:true`). Delegation therefore doubles the connections traffic
per run; the probe is not shared with the parent.

## The lever

`hermes_cli/config_defaults.py:1718-1726` — subagent threads ALWAYS resolve approvals
non-interactively; `delegation.subagent_auto_approve` default `false` means auto-DENY.
Both runs set it `true` (harness `--subagent-auto-approve`). `delegation.max_spawn_depth`
default `1` is sufficient for flat parent→child and was left at the default.

Caveat on attribution: neither run's child ever hit an approval prompt (the bridge tools
are not dangerous-command-gated), so **these runs do not prove the auto-approve flag was
load-bearing for the bridge path specifically.** It was set defensively. It remains the
right default to flip for delegation work, but the bridge failure in the build session
had a different cause — see finding 2 below.

## Ground truth on context_id inheritance

**The child does NOT reuse a parent `context_id` — because the parent never has one.**

- `agent/agent_init.py:1470` sets `agent._tool_provider_context_id = None`, and the
  session-init `/v1/connections` probe at `:1494` passes `context_id=None` and does not
  capture one back.
- `tools/delegate_tool.py:1566-1570` copies the parent's value to the child. The
  mechanism is correct but **vacuous** unless the parent itself called
  `tool_search`/`tool_call` before delegating. In both runs the parent's value was `None`
  at delegate time, so the child inherited `None` and minted its own.
- Run 1 child minted `trs_0y6UtT4z9t3M` (from `/v1/search`).
- Run 2 child's `context_id` was `1e2aa8cb-f64d-480d-9cec-814ec4a9eb1a` — **byte-identical
  to the gateway's `requestId` for that execute.** `/v1/execute` returns the per-request id,
  not a session-scoped token, so it changes on every call and cannot carry session identity.

Two endpoints, two incompatible `context_id` shapes (`trs_…` vs a request UUID).

## Run 1 (`delegate_bridge_child.txt`) — the search-first path fails

Same setup, child told to `tool_search` first. The child stayed on the bridge for
~15 turns (`tool_search` ×6, `app_connections` ×5, `tool_call` ×2, `tool_describe` ×1) and
never touched a terminal — but got **zero** real data. Preserved at `artifacts/run1_state.db`.
It hit three separate walls; see findings.

## Files

| file | what |
|---|---|
| `run_delegate_probe.sh` | the one rerunnable command |
| `correlate.py` | joins `state.db` sessions/messages against the gateway dev log |
| `../harness/scenarios/delegate_bridge_child_slug.txt` | the scenario that PASSES (slug-direct) |
| `../harness/scenarios/delegate_bridge_child.txt` | the scenario that fails (search-first) |
| `artifacts/run2_transcript.txt` | redacted passing transcript |
| `artifacts/run{1,2}_state.db` | session stores, secret-scanned clean |

`correlate.py` limitation: its `CTX_RE` only matches `trs_…`, so it misses the
`/v1/execute` UUID form. Read the raw `messages.content` for execute-path context ids.

## What a working demo requires

1. Isolated profile from the harness, `--subagent-auto-approve`, `max_spawn_depth ≥ 1`.
2. **Name the tool slug explicitly in the delegated goal.** `tool_search` cannot find
   hackernews (0 results, every phrasing), so a search-first child dead-ends. Verified
   slugs: `HACKERNEWS_GET_LATEST_POSTS` (returns titles), `HACKERNEWS_GET_TOP_STORIES`
   (ids only). `HACKERNEWS_GET_FRONTPAGE` does not exist.
3. Steer against curl/terminal explicitly (known edge 1). Both runs' children obeyed.
4. Fresh token — 900 s TTL, baked in at launch. Re-run `up`, then relaunch.
