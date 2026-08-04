"""Fan-out merge + graceful-degradation probes for the tool-search bridge.

Lens: tools/tool_search.py's dispatch_tool_search (local BM25 catalog fanned
out in parallel with tools/tool_provider_gateway.py's /v1/search) and its
sibling dispatch calls (tool_describe -> /v1/schemas, dispatch_provider_tool_call
-> /v1/execute).

Two halves:

(a) MERGE — local BM25 hits and gateway hits both present: dedup/ordering,
    total_available / limit accounting, inline gateway schemas surviving the
    merge, and what happens when a gateway slug collides with a local tool
    name.
(b) DEGRADATION — gateway 500, timeout, malformed JSON, 403
    SUBSCRIPTION_REQUIRED, and connection-refused. For each: tool_search
    still returns local results, never raises, and the failure text is
    asserted byte-for-byte.

All mockable — no live gateway needed for the assertions below (the fanout
README documents the one live sanity call this lens made separately).

Run:
    cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge
    TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 \
        .venv/bin/python -m pytest tests/integration_tool_provider/fanout/test_fanout_merge_degradation.py -q
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _td(name: str, description: str = "", properties: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties or {}},
        },
    }


def _register_local(name: str, description: str = "Search local GitHub issues",
                     toolset: str = "mcp-fanout-probe") -> Dict[str, Any]:
    from tools.registry import registry

    tool_def = _td(name, description)
    registry.register(
        name=name,
        handler=lambda args, **kwargs: "{}",
        schema=tool_def,
        toolset=toolset,
    )
    return tool_def


@pytest.fixture(autouse=True)
def _clean_registry():
    """Registered probe tools are process-global; drop them after each test.

    Mirrors the pattern in tests/tools/test_tool_search.py's
    TestProviderSearchFanout (no explicit teardown there either, but that
    file only ever adds — never re-registers — a given name). We go one
    step further and unregister so a bad interaction between tests in this
    file can't masquerade as a merge finding.
    """
    from tools.registry import registry

    before = set(registry._tools.keys()) if hasattr(registry, "_tools") else None
    yield
    if before is not None:
        for name in list(registry._tools.keys()):
            if name not in before:
                del registry._tools[name]


# ---------------------------------------------------------------------------
# (a) MERGE
# ---------------------------------------------------------------------------


class TestMergeOrdering:
    def test_matches_are_concatenated_not_interleaved(self, monkeypatch):
        """Design doc (docs/design/tool-provider-bridge.md) claims local and
        gateway hits are "interleaved". The implementation instead computes
        local hits first, then does `result["matches"].extend(gateway_hits)`
        — i.e. two contiguous blocks (all-local, then all-gateway), not an
        interleave by relevance/rank across sources. Pin the actual shape so
        a future change to real interleaving is a deliberate, visible diff.
        """
        from tools import tool_provider_gateway as gateway
        from tools import tool_search
        from tools.tool_provider_gateway import ProviderSearchResponse, SearchResultGroup, ToolRef

        local_a = _register_local("fanout_order_local_a", "GitHub issue tool A")
        local_b = _register_local("fanout_order_local_b", "GitHub issue tool B")
        monkeypatch.setattr(tool_search, "_tool_provider_gateway_config", lambda: object())

        async def fake_search(config, queries, *, context_id=None, model=None):
            return ProviderSearchResponse(
                context_id="ctx",
                results=[SearchResultGroup(
                    use_case="github",
                    tools=[
                        ToolRef(slug="GITHUB_TOOL_ONE", toolkit="github", description="one"),
                        ToolRef(slug="GITHUB_TOOL_TWO", toolkit="github", description="two"),
                    ],
                )],
            )

        monkeypatch.setattr(gateway, "search", fake_search)
        result = json.loads(tool_search.dispatch_tool_search(
            {"query": "github issue", "limit": 10},
            current_tool_defs=[local_a, local_b],
        ))

        names = [m["name"] for m in result["matches"]]
        sources = [m["source"] for m in result["matches"]]
        # Both locals precede both gateway hits — a block layout, not an
        # interleave. If this assertion ever flips to something like
        # [local, provider, local, provider], the design doc's claim has
        # actually been implemented and this test (and the doc-vs-code
        # discrepancy it documents) should be updated together.
        assert sources == ["mcp", "mcp", "provider", "provider"], sources
        assert names == [
            "fanout_order_local_a", "fanout_order_local_b",
            "GITHUB_TOOL_ONE", "GITHUB_TOOL_TWO",
        ]


class TestMergeLimitAndTotalAvailableAccounting:
    def test_limit_is_applied_per_source_not_to_the_merged_result(self, monkeypatch):
        """The tool_search bridge schema tells the model: 'Returns up to
        `limit` matches'. In fact `limit` caps the LOCAL hits and the
        GATEWAY hits independently, then both capped lists are concatenated
        — so a caller asking for limit=2 can receive up to 4 matches back
        when both sources have >= 2 relevant hits. This directly contradicts
        the bridge's own tool description text.
        """
        from tools import tool_provider_gateway as gateway
        from tools import tool_search
        from tools.tool_provider_gateway import ProviderSearchResponse, SearchResultGroup, ToolRef

        for i in range(3):
            _register_local(f"fanout_limit_local_{i}", f"GitHub issue tool {i}")
        monkeypatch.setattr(tool_search, "_tool_provider_gateway_config", lambda: object())

        async def fake_search(config, queries, *, context_id=None, model=None):
            return ProviderSearchResponse(
                context_id="ctx",
                results=[SearchResultGroup(
                    use_case="github",
                    tools=[ToolRef(slug=f"GITHUB_TOOL_{i}", toolkit="github") for i in range(3)],
                )],
            )

        monkeypatch.setattr(gateway, "search", fake_search)
        local_defs = [_td(f"fanout_limit_local_{i}", f"GitHub issue tool {i}") for i in range(3)]
        result = json.loads(tool_search.dispatch_tool_search(
            {"query": "github issue", "limit": 2},
            current_tool_defs=local_defs,
        ))

        # 2 local (capped) + 2 gateway (capped) == 4, not <= the requested 2.
        assert len(result["matches"]) == 4, result["matches"]

    def test_total_available_undercounts_gateway_matches_truncated_by_limit(self, monkeypatch):
        """`total_available` is built as `len(local_catalog) + len(gateway_hits)`.
        The local addend is the FULL local pool size (independent of
        `limit` — intentional, lets the model know more exist). The gateway
        addend is `gw_response.all_tools()[:limit]` — the TRUNCATED slice
        actually attached to this response, not the true number of gateway
        tools that matched. When the gateway returns more than `limit`
        tools, `total_available` silently undercounts the external side
        while still accurately counting the local side — an inconsistent
        "total available" signal the model can't use to decide whether to
        broaden its query.
        """
        from tools import tool_provider_gateway as gateway
        from tools import tool_search
        from tools.tool_provider_gateway import ProviderSearchResponse, SearchResultGroup, ToolRef

        local_def = _register_local("fanout_total_local", "GitHub issue tool")
        monkeypatch.setattr(tool_search, "_tool_provider_gateway_config", lambda: object())

        TRUE_GATEWAY_MATCH_COUNT = 5
        LIMIT = 2

        async def fake_search(config, queries, *, context_id=None, model=None):
            return ProviderSearchResponse(
                context_id="ctx",
                results=[SearchResultGroup(
                    use_case="github",
                    tools=[ToolRef(slug=f"GITHUB_TOOL_{i}", toolkit="github")
                           for i in range(TRUE_GATEWAY_MATCH_COUNT)],
                )],
            )

        monkeypatch.setattr(gateway, "search", fake_search)
        result = json.loads(tool_search.dispatch_tool_search(
            {"query": "github issue", "limit": LIMIT},
            current_tool_defs=[local_def],
        ))

        local_pool_size = 1  # one local tool registered
        reported = result["total_available"]
        true_total = local_pool_size + TRUE_GATEWAY_MATCH_COUNT
        assert reported == local_pool_size + LIMIT  # 1 + 2 == 3
        assert reported != true_total  # 3 != 6 — undercounts by exactly (5 - 2)
        assert true_total - reported == TRUE_GATEWAY_MATCH_COUNT - LIMIT


class TestMergeInlineSchemaSurvival:
    def test_inline_input_schema_passes_through_verbatim(self, monkeypatch):
        from tools import tool_provider_gateway as gateway
        from tools import tool_search
        from tools.tool_provider_gateway import ProviderSearchResponse, SearchResultGroup, ToolRef

        monkeypatch.setattr(tool_search, "_tool_provider_gateway_config", lambda: object())
        schema = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}

        async def fake_search(config, queries, *, context_id=None, model=None):
            return ProviderSearchResponse(
                context_id="ctx",
                results=[SearchResultGroup(
                    use_case="github",
                    tools=[ToolRef(slug="GITHUB_CREATE_ISSUE", toolkit="github",
                                   input_schema=schema)],
                )],
            )

        monkeypatch.setattr(gateway, "search", fake_search)
        result = json.loads(tool_search.dispatch_tool_search(
            {"query": "create issue"}, current_tool_defs=[],
        ))
        hit = next(m for m in result["matches"] if m["source"] == "provider")
        assert hit["input_schema"] == schema

    def test_missing_input_schema_omits_the_key_rather_than_null(self, monkeypatch):
        from tools import tool_provider_gateway as gateway
        from tools import tool_search
        from tools.tool_provider_gateway import ProviderSearchResponse, SearchResultGroup, ToolRef

        monkeypatch.setattr(tool_search, "_tool_provider_gateway_config", lambda: object())

        async def fake_search(config, queries, *, context_id=None, model=None):
            return ProviderSearchResponse(
                context_id="ctx",
                results=[SearchResultGroup(
                    use_case="github",
                    tools=[ToolRef(slug="GITHUB_CREATE_ISSUE", toolkit="github")],
                )],
            )

        monkeypatch.setattr(gateway, "search", fake_search)
        result = json.loads(tool_search.dispatch_tool_search(
            {"query": "create issue"}, current_tool_defs=[],
        ))
        hit = next(m for m in result["matches"] if m["source"] == "provider")
        assert "input_schema" not in hit


class TestMergeSlugCollision:
    """A gateway slug identical to a local tool name — document what actually
    happens, per the task brief. The routing heuristic (is_provider_tool_name)
    already special-cases this for tool_call routing (registry entry wins);
    this class checks the two paths that heuristic does NOT cover: the
    tool_search results list, and tool_describe resolution.
    """

    def test_search_lists_the_colliding_name_twice_with_different_sources(self, monkeypatch):
        from tools import tool_provider_gateway as gateway
        from tools import tool_search
        from tools.tool_provider_gateway import ProviderSearchResponse, SearchResultGroup, ToolRef

        colliding_name = "GITHUB_CREATE_ISSUE"
        local_def = _register_local(colliding_name, "Locally registered, same name as a gateway slug")
        monkeypatch.setattr(tool_search, "_tool_provider_gateway_config", lambda: object())

        async def fake_search(config, queries, *, context_id=None, model=None):
            return ProviderSearchResponse(
                context_id="ctx",
                results=[SearchResultGroup(
                    use_case="github",
                    tools=[ToolRef(slug=colliding_name, toolkit="github",
                                   description="Gateway's own version of the same name")],
                )],
            )

        monkeypatch.setattr(gateway, "search", fake_search)
        result = json.loads(tool_search.dispatch_tool_search(
            {"query": "create issue"}, current_tool_defs=[local_def],
        ))

        hits = [m for m in result["matches"] if m["name"] == colliding_name]
        # No dedup by name anywhere in the merge step: the same name shows up
        # once per source, with different `source`/description fields, and
        # the model has no signal that these two entries denote the SAME
        # identifier in two different dispatch tables.
        assert len(hits) == 2, hits
        assert {h["source"] for h in hits} == {"mcp", "provider"}

    def test_describe_resolves_to_the_local_definition_gateway_homonym_unreachable(self, monkeypatch):
        from tools import tool_provider_gateway as gateway
        from tools import tool_search

        colliding_name = "GITHUB_CREATE_ISSUE"
        local_def = _register_local(colliding_name, "LOCAL description should win")

        async def fake_schemas(config, slugs, *, context_id=None, include_output=False):
            pytest.fail(
                "tool_describe reached the gateway for a name shadowed by a "
                "local registry entry — the local definition should have "
                "won without a network call"
            )

        monkeypatch.setattr(gateway, "schemas", fake_schemas)
        result = json.loads(tool_search.dispatch_tool_describe(
            {"name": colliding_name}, current_tool_defs=[local_def],
        ))
        # Local wins deterministically; the gateway's homonymous tool is
        # silently and permanently unreachable via tool_describe. No error,
        # no warning — just quiet shadowing.
        assert result["description"] == "LOCAL description should win"


# ---------------------------------------------------------------------------
# (b) DEGRADATION
# ---------------------------------------------------------------------------


class TestSearchDegradesToLocalResults:
    """For each gateway failure mode: dispatch_tool_search must not raise,
    must still return the local hits, and the model-visible failure text is
    pinned exactly (and checked for the absence of vendor-identifying
    substrings a real 502 envelope is documented to carry — see the
    "KNOWN-OPEN gateway defects" note this probe was briefed on).
    """

    @staticmethod
    def _dispatch_with_failing_search(monkeypatch, exc):
        from tools import tool_provider_gateway as gateway
        from tools import tool_search

        local_def = _register_local("fanout_degrade_local", "local github search")
        monkeypatch.setattr(tool_search, "_tool_provider_gateway_config", lambda: object())

        async def failing_search(*args, **kwargs):
            raise exc

        monkeypatch.setattr(gateway, "search", failing_search)
        raw = tool_search.dispatch_tool_search(
            {"query": "github issue"}, current_tool_defs=[local_def], context_id="ctx-keep",
        )
        return json.loads(raw)

    def test_gateway_500_unreadable_body(self, monkeypatch):
        # Exact string _post() raises for a >=400 status whose body isn't an
        # {"error": {...}} shape (tools/tool_provider_gateway.py:154-156).
        from tools.tool_provider_gateway import ToolProviderTransportError
        exc = ToolProviderTransportError(
            "tools-gateway returned HTTP 500 with an unreadable body"
        )
        result = self._dispatch_with_failing_search(monkeypatch, exc)
        assert [m["name"] for m in result["matches"]] == ["fanout_degrade_local"]
        assert result["gateway_notice"] == (
            "External app-tool search unavailable this turn: "
            "tools-gateway returned HTTP 500 with an unreadable body"
        )
        assert result["context_id"] == "ctx-keep"  # preserved, not clobbered

    def test_gateway_timeout(self, monkeypatch):
        from tools.tool_provider_gateway import ToolProviderTransportError
        exc = ToolProviderTransportError("TimeoutException: request timed out")
        result = self._dispatch_with_failing_search(monkeypatch, exc)
        assert [m["name"] for m in result["matches"]] == ["fanout_degrade_local"]
        assert result["gateway_notice"] == (
            "External app-tool search unavailable this turn: "
            "TimeoutException: request timed out"
        )

    def test_gateway_malformed_json_body(self, monkeypatch):
        # Exact string _post() raises when response.json() fails on a
        # <400 status (tools/tool_provider_gateway.py:158-159).
        from tools.tool_provider_gateway import ToolProviderTransportError
        exc = ToolProviderTransportError("tools-gateway returned a non-JSON response body")
        result = self._dispatch_with_failing_search(monkeypatch, exc)
        assert [m["name"] for m in result["matches"]] == ["fanout_degrade_local"]
        assert result["gateway_notice"] == (
            "External app-tool search unavailable this turn: "
            "tools-gateway returned a non-JSON response body"
        )

    def test_gateway_403_subscription_required(self, monkeypatch):
        from tools.tool_provider_gateway import ToolProviderGatewayError
        exc = ToolProviderGatewayError("SUBSCRIPTION_REQUIRED", "Subscribe to use external app tools")
        result = self._dispatch_with_failing_search(monkeypatch, exc)
        assert [m["name"] for m in result["matches"]] == ["fanout_degrade_local"]
        assert result["gateway_notice"] == (
            "External app-tool search unavailable this turn: "
            "Subscribe to use external app tools"
        )
        # 403s are structured ApiErrors, not raw prose — no vendor-identifying
        # substring is expected here, and none is present.
        assert "code" not in result  # dispatch_tool_search doesn't surface .code on search

    def test_connection_refused(self, monkeypatch):
        # Exact string _post() raises for any non-HTTP transport exception
        # (tools/tool_provider_gateway.py:131-132): f"{type(exc).__name__}: {exc}".
        from tools.tool_provider_gateway import ToolProviderTransportError
        exc = ToolProviderTransportError("ConnectError: [Errno 111] Connection refused")
        result = self._dispatch_with_failing_search(monkeypatch, exc)
        assert [m["name"] for m in result["matches"]] == ["fanout_degrade_local"]
        assert result["gateway_notice"] == (
            "External app-tool search unavailable this turn: "
            "ConnectError: [Errno 111] Connection refused"
        )

    def test_generic_unexpected_exception_also_degrades(self, monkeypatch):
        """The bare `except Exception` fallback (tool_search.py:1009) — proves
        even an exception type neither gateway module raises can't take down
        the whole tool_search call.
        """
        result = self._dispatch_with_failing_search(monkeypatch, ValueError("boom"))
        assert [m["name"] for m in result["matches"]] == ["fanout_degrade_local"]
        assert result["gateway_notice"] == "External app-tool search unavailable this turn: boom"


class TestErrorMessagePassthroughHasNoRedaction:
    """Mechanism-level finding: none of the three gateway-dispatch exception
    handlers in tools/tool_search.py sanitize or redact the upstream message
    before it becomes model-visible tool-result text. This matters because
    the gateway's error envelope is documented (this probe's briefing) as a
    KNOWN-OPEN, already-specced-to-fix defect: "the 502 error envelope names
    the vendor and echoes raw upstream prose". Whatever that upstream prose
    says today reaches the model verbatim through THIS bridge with no
    intervening filter — these tests prove the verbatim-relay mechanism using
    an arbitrary placeholder marker (never the real vendor string), so if the
    upstream message ever does carry vendor-identifying text, it flows
    straight through unredacted rather than being caught by a bridge-side
    safety net that doesn't exist.
    """

    _MARKER = "UPSTREAM-PROSE-MARKER-7f2c"  # stand-in for any raw upstream text

    def test_search_gateway_notice_relays_message_verbatim(self, monkeypatch):
        from tools import tool_provider_gateway as gateway
        from tools import tool_search
        from tools.tool_provider_gateway import ToolProviderGatewayError

        local_def = _register_local("fanout_passthrough_local")
        monkeypatch.setattr(tool_search, "_tool_provider_gateway_config", lambda: object())

        async def failing_search(*args, **kwargs):
            raise ToolProviderGatewayError("UPSTREAM_ERROR", self._MARKER)

        monkeypatch.setattr(gateway, "search", failing_search)
        result = json.loads(tool_search.dispatch_tool_search(
            {"query": "issue"}, current_tool_defs=[local_def],
        ))
        assert self._MARKER in result["gateway_notice"]
        assert result["gateway_notice"] == (
            f"External app-tool search unavailable this turn: {self._MARKER}"
        )

    def test_execute_tool_error_relays_message_verbatim(self, monkeypatch):
        from tools import tool_provider_gateway as gateway
        from tools import tool_search
        from tools.tool_provider_gateway import ToolProviderGatewayError

        monkeypatch.setattr(tool_search, "_tool_provider_gateway_config", lambda: object())

        async def failing_execute(*args, **kwargs):
            raise ToolProviderGatewayError("UPSTREAM_ERROR", self._MARKER)

        monkeypatch.setattr(gateway, "execute", failing_execute)
        result = json.loads(tool_search.dispatch_provider_tool_call(
            "GITHUB_CREATE_ISSUE", {"title": "x"},
        ))
        assert self._MARKER in result["error"]
        assert result["error"] == f"GITHUB_CREATE_ISSUE: {self._MARKER}"

    def test_describe_does_not_relay_raw_exception_text(self, monkeypatch):
        """The one bridge dispatch path that DOESN'T leak: tool_describe
        swallows the gateway exception (logger.debug only) and falls through
        to a fixed, generic error string — the safe pattern the other two
        paths lack.
        """
        from tools import tool_provider_gateway as gateway
        from tools import tool_search

        monkeypatch.setattr(tool_search, "_tool_provider_gateway_config", lambda: object())

        async def failing_schemas(*args, **kwargs):
            raise RuntimeError(self._MARKER)

        monkeypatch.setattr(gateway, "schemas", failing_schemas)
        result = json.loads(tool_search.dispatch_tool_describe(
            {"name": "GITHUB_CREATE_ISSUE"}, current_tool_defs=[],
        ))
        assert self._MARKER not in json.dumps(result)
        assert result["error"] == (
            "'GITHUB_CREATE_ISSUE' is not currently available. Re-run tool_search to refresh."
        )
