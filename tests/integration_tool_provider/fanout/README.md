# Fan-out merge + graceful-degradation probes

Lens: `tools/tool_search.py`'s `dispatch_tool_search` (local BM25 catalog fanned out
in parallel with `tools/tool_provider_gateway.py`'s `/v1/search`), plus its sibling
dispatch calls (`dispatch_tool_describe` -> `/v1/schemas`, `dispatch_provider_tool_call`
-> `/v1/execute`).

## Run

```
cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge
TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 \
    .venv/bin/python -m pytest tests/integration_tool_provider/fanout/test_fanout_merge_degradation.py -q
```

16 tests, all mocked (`tools.tool_provider_gateway.search/schemas/execute` monkeypatched),
no live gateway required. Runtime ~0.6s.

## What's covered

**Merge** (`TestMergeOrdering`, `TestMergeLimitAndTotalAvailableAccounting`,
`TestMergeInlineSchemaSurvival`, `TestMergeSlugCollision`):

- Local and gateway hits are concatenated in two contiguous blocks (all-local then
  all-gateway), not interleaved by relevance — contradicts the design doc's "interleaved"
  claim (`docs/design/tool-provider-bridge.md` line 19).
- `limit` caps each source independently, then both capped lists are concatenated — a
  caller asking for `limit=2` can get back up to 4 matches (2 local + 2 gateway), despite
  the bridge tool's own description text promising "up to `limit` matches".
- `total_available` mixes two different semantics: the local addend is the untruncated
  pool size, the gateway addend is the post-`limit`-truncation count actually attached to
  the response (not the true number of gateway matches) — so it silently undercounts the
  external side whenever a query matches more gateway tools than `limit`.
- Inline `input_schema` from the gateway survives the merge verbatim; when absent, the key
  is omitted rather than set to `null`.
- A gateway slug identical to a local tool name is NOT deduped in `tool_search` results —
  it appears twice, once per source, with independent descriptions. `tool_describe`
  resolves the collision deterministically to the local definition (matches
  `is_provider_tool_name`'s registry-presence check) — the gateway's homonymous tool
  becomes silently and permanently unreachable via describe, with no error or warning.

**Degradation** (`TestSearchDegradesToLocalResults`, `TestErrorMessagePassthroughHasNoRedaction`):

- Five failure modes (500/unreadable body, timeout, malformed JSON body, 403
  `SUBSCRIPTION_REQUIRED`, connection-refused) plus a generic unexpected-exception
  fallback — `dispatch_tool_search` never raises, local results survive untouched, and
  the exact model-visible `gateway_notice` string is pinned for each.
- Mechanism-level finding: none of the three gateway-dispatch exception handlers in
  `tool_search.py` redact or sanitize the upstream message before it becomes model-visible
  tool-result text (`gateway_notice` for search, `error` for execute). Proven with an
  arbitrary placeholder marker string rather than a real vendor-identifying string.
  `tool_describe` is the one path that's safe — it swallows the exception and falls
  through to a fixed generic string.

## One live sanity call (not in the pytest file — recorded here)

`POST /v1/search` against the live gateway, query `"create a github issue"`:

- HTTP 200, latency ~3.1s (matches the wayfinder's noted 2.5-4.6s search range).
- Top-level keys: `connections`, `context_id`, `guidance`, `results` — matches
  `ProviderSearchResponse`'s parser exactly.
- One result group, 7 tools, each with `slug`/`toolkit`/`description`/`connected`/
  `input_schema` keys — matches `ToolRef`'s parser exactly, and empirically confirms the
  `total_available`/`limit` finding above: a single query against a single group already
  returned more tools (7) than the bridge's default search limit (5) would keep.
- No provider/vendor-identifying substrings in the response body (checked by grep).
