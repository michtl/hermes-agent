# Wire sweep: cross-surface vendor scan + latency envelope

Pure HTTP lens over the tool-provider gateway's four `/v1` routes (`search`,
`schemas`, `execute`, `connections`). No CLI, no browser, no hermes-agent
process — direct wire calls, chosen specifically because they answer both
questions this area owns (does the vendor name leak anywhere on the wire,
and what does the live latency envelope look like) faster than driving a
full agent session would.

Per house rule, **the vendor name is never printed** in this README, in
`LATENCY.md`, or in anything under `artifacts/`. `vendor_scan.py` contains
the literal exactly once, as the documented exception: it is the scan
pattern the whole harness exists to match against, and every hit the module
returns has the matched substring masked (`[VENDOR]`) before it leaves the
function. Everything downstream — persisted response artifacts, the
findings file, this README — only ever sees the redacted form.

## Rerun (one command)

```bash
cd tests/integration_tool_provider/wire-sweep
../../../.venv/bin/python sweep.py
```

Mints its own NAS dev JWT at the start of the run (900s TTL, plenty for the
~47 requests this makes), so nothing needs a hand-copied token. Exit code is
`0` if no *new* model-visible vendor hit was found, `1` otherwise (existing
known-open issues, see below, do not affect the exit code).

Optional env overrides: `NAS_BASE` (default `http://127.0.0.1:3111`),
`GATEWAY_BASE` (default `http://tools-gateway.localhost:3009`).

There is also a fast, network-free pytest suite for the pure recursive
scanner (no live calls, run this first / in CI):

```bash
cd <repo root>
.venv/bin/python -m pytest tests/integration_tool_provider/wire-sweep/test_vendor_scanner.py -q
```

10/10 passing as of this writing.

## What `sweep.py` does

1. Mints a token, then fires ~22 shaped requests across the sample plan
   below (several toolkits, broad + narrow queries, success + error paths),
   plus 25 more timed requests for the latency envelope (5 samples × 5
   route/shape combinations) — 47 requests total, one run, a few seconds
   to ~1.5 minutes wall clock (search calls are the slow ones, ~3s each).
2. Every response body is recursively walked (`vendor_scan.find_vendor_hits`)
   — dict keys and values, list elements, arbitrary nesting depth — so it
   catches the easy-to-miss spots the brief called out (`plan`, `pitfalls`,
   `guidance`, per-tool `error` strings, `connections` entries, schema
   `description`/property names) without hardcoding a field list.
3. Every hit is classified `accepted_boundary` (a real OAuth `connect_url`
   host — the one place the vendor's domain legitimately has to appear,
   since a client points a real browser at it) or `model_visible_defect`
   (anything else — this is a wire response an agent harness or a human
   reading the dashboard would see).
4. Hits are cross-checked against known-edge #6 (see below) so the sweep
   doubles as that edge's acceptance test, without re-reporting a fix-pending
   issue as new.
5. Response bodies, per-call vendor-hit counts, and classified findings are
   written to `artifacts/` (vendor-redacted). Latency samples are written to
   `artifacts/latency.json` and rendered as a p50/max table in `LATENCY.md`.

## Sample plan

- **search**: broad natural-language queries against hackernews, slack,
  github, googlecalendar, gmail, notion; one narrow/slug-shaped query;
  error paths for empty `queries` and `queries` over the 5-item cap.
- **schemas**: a real 9-slug hackernews batch (hand-discovered via
  `/v1/schemas` probing — the search route returns 0 results for
  HackerNews-shaped queries, known edge #2, so schemas had to be reached
  directly); a disabled-toolkit slug (`TOOLKIT_NOT_ENABLED`); a
  well-formed-but-nonexistent slug (`TOOL_NOT_FOUND`).
- **execute**: a single real hackernews call, a real ~10-tool hackernews
  batch (all genuinely executed, no auth needed), a disabled-toolkit slug,
  a tool-level bad-argument call (exercises the verbatim-passthrough error
  path, not the envelope), and a context-id-echo probe (see known edges).
- **connections**: `status` for all enabled toolkits, `connect` for github
  and slack (both mint real `connect_url`s), and a disabled-toolkit `status`
  call.
- Plus one deliberate bad-JWT call against `search` for the auth-error path.

## Findings

### New vendor leak (not in known edge #6)

`POST /v1/search` for a broad Slack query returns a `SLACK_FIND_CHANNELS`
tool whose `description` field — part of `ToolRef`, which the contract says
is passed through into the model-visible `SearchResponse` envelope — names
the vendor in a diagnostic instruction to the model: it tells the model to
check a vendor-branded response field name (masked here as
`'[VENDOR]_execution_message'`) for troubleshooting details. This reaches
the agent directly in a 200 response on a completely ordinary query; no
error path or edge case is needed to trigger it. See
`artifacts/responses/search__broad-slack.json`,
`$.results[0].tools[0].description`, and
`artifacts/findings.json.genuinely_new_hits_not_covered_by_known_edge_6`.

This is a tool-catalog-description leak, not a plan/pitfalls/error-envelope
leak, so it sits outside the four items enumerated in known edge #6 — it is
reported here as new, not re-litigated as one of those.

### Known edge #6 status (acceptance-test read, not new findings)

| Item | Reproduced this run? | Evidence |
|---|---|---|
| 502 `UPSTREAM_ERROR` envelope names the vendor + echoes raw upstream prose | Not reproduced | Could not force a 502 from black-box wire calls within the time budget (nonexistent-tool and disabled-toolkit slugs both resolved to clean 200/403s, not 502 — see `artifacts/responses/execute__error-tool-level-bad-arg.json` and `execute__error-toolkit-not-enabled.json`). Absence of a repro here is not evidence the fix landed, only that this probe didn't hit the path. |
| `plan`/`pitfalls` arrays leak vendor strings | Not reproduced | All 6 broad-toolkit search samples' `plan`/`pitfalls` arrays came back clean (see `test_vendor_scanner`-style scan over `artifacts/responses/search__*.json` — 0 hits in any `.plan`/`.pitfalls` path). Consistent with this having been fixed since the spec was written; the *description* field leak above is a distinct, unfixed spot. |
| `/v1/execute` echoes a caller-supplied `context_id` verbatim | **Reproduced** | Sent `context_id: "bogus-ctx-injected-marker-12345"` on an execute call; response `context_id` came back identical, even though the contract says session resolution is by `userId` alone and a foreign `context_id` should simply never be consulted. See `artifacts/responses/execute__context-id-echo-probe.json`. |
| NAS `toolOverrides` neither persisted by NAS nor enforced by the gateway | Not probed | Verifying this needs mutating the org's toolkit policy, which this read-only wire-sweep lens is not authorized to do (and the task didn't ask for it here). Left as not-probed rather than guessed. |

### Accepted boundary (not a leak)

Both `connections` `action="connect"` calls (github, slack) returned a real
OAuth `connect_url` on the vendor's connect domain
(`https://connect.[VENDOR].dev/link/...`) — this is the one place the name
has to appear on the wire since a real browser gets pointed at that host.
Classified `accepted_boundary`, not counted toward the defect total.

## Latency envelope

See `LATENCY.md` (committed, regenerated by every `sweep.py` run). Headline:

- **search**: p50 3.18s, max 3.30s — within the observed 2.5–4.6s envelope.
- **schemas** (9-slug batch): p50 0.53s, max 0.78s — no observed baseline
  was given for this route to compare against.
- **execute, single tool**: p50 0.90s, max 1.57s — p50 sits comfortably
  inside the observed ~1.0–1.3s envelope; the single 1.57s max sample is
  ~0.27s over the observed high but didn't recur across the other 4
  samples, so it reads as a one-off wobble, not a trend.
- **execute, 10-tool batch**: p50 2.02s, max 2.47s — flagged as a
  regression against the observed ~1.0–1.3s envelope (p50 delta +0.72s).
  Caveat worth weighing: the observed baseline's tool-count isn't specified,
  so part of this gap may just be batch-size scaling (10 tools) rather than
  a genuine regression at matched batch size — flagging it as-is since the
  brief's comparison baseline doesn't distinguish, and roughly 2x latency
  for exactly 10x the tools is itself a reasonable data point either way.
- **connections status**: p50 0.45s, max 0.48s — no observed baseline was
  given for this route to compare against.

## Files

- `vendor_scan.py` — pure, network-free recursive scanner + redaction +
  classification. Contains the vendor literal exactly once (the scan
  pattern itself).
- `test_vendor_scanner.py` — 10 fast pytest unit tests for the above, no
  network, runtime-constructed vendor literal (never appears as source
  text) so it also exercises real-string matching end to end.
- `sweep.py` — the live driver described above.
- `artifacts/responses/*.json` — one file per live request: route, label,
  HTTP status, elapsed seconds, vendor-redacted body, hit count.
- `artifacts/findings.json` — classified vendor hits + known-edge-6 status,
  machine-readable.
- `artifacts/latency.json` — raw per-route timing samples.
- `LATENCY.md` — the committed p50/max table.
