# scenarios — live CLI, the steered no-auth loop + the connect wall

Lens: drive the real hermes CLI against the live local stack and prove BOTH sides —
what the transcript shows, and what the gateway actually served.

## One command

```bash
bash /home/daimon/github/hermes-agent/.worktrees/composio-bridge/tests/integration_tool_provider/scenarios/run_scenarios.sh
```

It stands the isolated profile up via the shared harness (`up` + `doctor`,
`HERMES_HOME` under `/tmp/hh-scen`, never the real `~/.hermes`), runs the three
scenario files below through `hermes -z`, and slices the gateway dev log by line
offset into `artifacts/gateway-*.log`. Override the log path with `GWLOG=...` if
the stack was relaunched under a different scratch dir.

The harness worked as documented. No fallback to the inline recipe was needed.

## Scenarios

| file | asks for | result (2026-08-04, live) |
|---|---|---|
| `noauth_loop.txt` | tool_search → tool_call, HackerNews top stories | **discovery fails**: 5 × `/v1/search`, every one `resultCount: 0`; model reports `LOOP_OK 0` |
| `noauth_execute.txt` | tool_call `HACKERNEWS_GET_LATEST_POSTS` directly | **execute works**: 1 × `/v1/execute` 200 in 1220ms, 5 real posts, `EXEC_OK 5` |
| `unconnected_github.txt` | GitHub repos (auth-required, unconnected) | **blocked, actionable**: connect URL + poll instructions surfaced to the user, `GH_RESULT blocked` |

The split between rows 1 and 2 is the headline. `/v1/schemas` resolves
`HACKERNEWS_GET_LATEST_POSTS`, `HACKERNEWS_GET_USER`,
`HACKERNEWS_GET_ITEM_WITH_ID`, `HACKERNEWS_SEARCH_POSTS`, and `/v1/execute`
runs them — but `/v1/search` returns zero for every HackerNews phrasing tried
(`hacker news top stories`, `hackernews frontpage`, `get hackernews user`,
`hacker news front page top stories`, `hackernews frontpage get top stories`).
The demo's discovery step is the broken link, not the execution step.
(`HACKERNEWS_GET_FRONTPAGE` does **not** exist — `/v1/schemas` answers
`TOOL_NOT_FOUND`.)

## Proof that the data is real, not hallucinated

Transcript 02 reports objectID `49173948` under story_title `"Web security is too hard"`.
Checked independently against the public HN API (a call this harness makes itself,
outside the bridge — `artifacts/hn-verify-*.json`):

```
https://hn.algolia.com/api/v1/items/49173948 -> id=49173948 type=comment story_id=49172834
https://hn.algolia.com/api/v1/items/49172834 -> type=story  title='Web security is too hard'  points=98  author=kevincox
```

Exact match on id, story_id and title. The model also correctly said "points: not
present in response" — these hits are comments, which carry no `points` field.
It did not invent one.

## Latency, measured

| stage | observed | prior envelope | note |
|---|---|---|---|
| session-init `/v1/connections` probe | 388–494 ms (5 samples) | ~0.5 s | in band |
| `/v1/search` (HackerNews, 0 results) | 2.1 / 2.4 / 2.7 / 2.9 / 3.0 / 3.8 s | 2.5–4.6 s | in band |
| `/v1/search` (GitHub, 1 result) | **6.4 s** | 2.5–4.6 s | **above band** — a hit costs more than a miss |
| `/v1/execute` (1 tool, hackernews) | 1220 ms (providerMs 1164) | ~1–1.3 s | in band |
| full steered turn — execute | 27.1 s | 26–30 s | in band |
| full steered turn — search only | 27.6 s | 26–30 s | in band |
| full steered turn — unconnected github | **77.9 s** | 26–30 s | **3×** — the bridge's own guidance text tells the model to poll status 6× at ~10 s intervals; the model obeyed. Cost is by design, but it is what a user waits through when a toolkit is unconnected. |

## Connections: the honest boundary

Checked first, as instructed. `/v1/connections action=status` for all six enabled
toolkits, at the top of this pass:

```
hackernews: disconnected   github: pending   googlecalendar: disconnected
gmail: disconnected        notion: disconnected   slack: pending
```

**No toolkit has an ACTIVE connection.** `pending` on github/slack means a connect
link was minted earlier and never walked through. So there is **no real
post-connect execution in this pass** — not faked, not simulated. The connect
wall is where it stops (known edge #3).

The connect URL is live. `action=connect` on `github` returns
`https://connect.composio.dev/link/lk_…`; fetched headlessly it answers HTTP 200
after one redirect to `https://dashboard.composio.dev/link/lk_…` — a real
interstitial, one click short of the GitHub OAuth wall. Stopped there.

## Correlation caveat

Sibling agents were driving the same stack against the same seeded principal
during this window, so gateway log lines are attributed by **timestamp window +
call sequence**, not by a per-session id. The `/v1/execute` at 19:42:58.335 is
unambiguous (it is the only execute in the whole pass) and its shape —
`toolCount:1 toolkitCount:1 toolkits:["hackernews"]` — matches the scenario
exactly. Slices may contain a stray neighbouring line; the marked windows are in
`artifacts/`.

## Artifacts

```
artifacts/transcript-01-search-only.txt        LOOP_OK 0
artifacts/transcript-02-execute.txt            EXEC_OK 5   <- the money shot
artifacts/transcript-03-unconnected-github.txt GH_RESULT blocked
artifacts/gateway-01-search-only.log           5x /v1/search resultCount:0
artifacts/gateway-02-execute.log               the billable execute + its 200
artifacts/gateway-03-unconnected-github.log    connect + 6 status polls
artifacts/hn-verify-49173948.json              independent public-API proof
artifacts/hn-verify-49172834.json
```

All transcripts carry the harness's `# redaction self-check: CLEAN` line, and the
whole directory was re-grepped for JWT / `sk-` / `ak_` / `oak_` shapes: clean.
