"""LENS: tool_call dispatch of gateway (Composio) slugs through model_tools.py.

Covers:
  (a) The TWO insertion points in model_tools.handle_function_call that route
      a provider (gateway) slug to tools.tool_search.dispatch_provider_tool_call:
        1. the tool_call BRIDGE branch (function_name == "tool_call", the
           model wraps the slug as {"name": slug, "arguments": {...}}) --
           ~model_tools.py:1205, inside the bridge-dispatch block that runs
           BEFORE any middleware/hook wiring is set up.
        2. the DIRECT branch (function_name == the slug itself) --
           ~model_tools.py:1362, the `elif is_provider_tool_name(function_name)`
           arm of the normal dispatch-function selection, which runs AFTER
           tool-request middleware and the pre_tool_call hook, and whose
           result is wrapped in run_tool_execution_middleware / post_tool_call
           / transform_tool_result like any other tool.
      Both must thread arguments and context_id to the gateway call unchanged.

  (b) Guardrail/middleware parity between the two insertion points. This is
      the interesting finding: they are NOT equivalent. Insertion point 2 goes
      through the full hook/middleware surface; insertion point 1 does not.
      See TestProviderToolCallHookParity for the proof and the production
      context that currently masks it.

  (c) The SCREAMING_SNAKE routing heuristic (tools.tool_search.is_provider_tool_name)
      exercised through the real dispatch (not just the predicate in isolation):
      a locally-registered SCREAMING_SNAKE tool, an unknown SCREAMING_SNAKE
      name, and a lowercase gateway-slug-shaped name.

Run (per repo convention -- per-file, not the full suite):
    cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge
    TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 .venv/bin/python -m pytest \
        tests/integration_tool_provider/dispatch/test_tool_call_dispatch.py -q
"""

import json

import pytest


# ---------------------------------------------------------------------------
# (a) Both insertion points thread arguments + context_id to the gateway call
# ---------------------------------------------------------------------------


class TestBothInsertionPointsThreadArgsAndContextId:
    """FAILS if either of model_tools.py's two provider-slug insertion points
    regresses: stops calling dispatch_provider_tool_call, drops the
    arguments, drops context_id, or accidentally falls through to
    registry.dispatch (which would mean a gateway slug tried to execute as
    a local tool and blow up with an unknown-tool error)."""

    @pytest.fixture(autouse=True)
    def _patch_provider_heuristic(self, monkeypatch):
        from tools import tool_search

        monkeypatch.setattr(
            tool_search,
            "is_provider_tool_name",
            lambda name: name == "GITHUB_CREATE_ISSUE",
        )

    def _capture_dispatch(self, monkeypatch):
        from tools import tool_search

        calls = []

        def fake_dispatch(name, arguments, *, context_id=None):
            calls.append({"name": name, "arguments": dict(arguments), "context_id": context_id})
            return json.dumps({"success": True, "context_id": context_id, "data": {"echo": arguments}})

        monkeypatch.setattr(tool_search, "dispatch_provider_tool_call", fake_dispatch)

        from tools.registry import registry

        monkeypatch.setattr(
            registry,
            "dispatch",
            lambda *a, **k: pytest.fail(
                f"provider slug fell through to registry.dispatch{a!r}{k!r} "
                "-- should have routed to the gateway"
            ),
        )
        return calls

    def test_insertion_point_1_tool_call_bridge(self, monkeypatch):
        """function_name == 'tool_call', arguments wrap {name, arguments}."""
        import model_tools

        calls = self._capture_dispatch(monkeypatch)

        result = model_tools.handle_function_call(
            function_name="tool_call",
            function_args={
                "name": "GITHUB_CREATE_ISSUE",
                "arguments": {"repo": "acme/widgets", "title": "bug"},
            },
            context_id="ctx-bridge-1",
        )

        assert calls == [{
            "name": "GITHUB_CREATE_ISSUE",
            "arguments": {"repo": "acme/widgets", "title": "bug"},
            "context_id": "ctx-bridge-1",
        }]
        assert json.loads(result)["success"] is True

    def test_insertion_point_2_direct_slug(self, monkeypatch):
        """function_name IS the gateway slug directly (no tool_call wrapper)."""
        import model_tools

        calls = self._capture_dispatch(monkeypatch)

        result = model_tools.handle_function_call(
            function_name="GITHUB_CREATE_ISSUE",
            function_args={"repo": "acme/widgets", "title": "bug"},
            context_id="ctx-direct-2",
        )

        assert calls == [{
            "name": "GITHUB_CREATE_ISSUE",
            "arguments": {"repo": "acme/widgets", "title": "bug"},
            "context_id": "ctx-direct-2",
        }]
        assert json.loads(result)["success"] is True

    def test_both_insertion_points_agree(self, monkeypatch):
        """Same slug/args/context_id through both shapes must reach the
        gateway identically -- this is the invariant tool_executor.py's
        two duplicated unwrap blocks (and model_tools.py's own bridge
        branch) all depend on."""
        import model_tools

        calls = self._capture_dispatch(monkeypatch)
        args = {"repo": "acme/widgets", "title": "bug"}

        model_tools.handle_function_call(
            function_name="tool_call",
            function_args={"name": "GITHUB_CREATE_ISSUE", "arguments": args},
            context_id="ctx-parity",
        )
        model_tools.handle_function_call(
            function_name="GITHUB_CREATE_ISSUE",
            function_args=args,
            context_id="ctx-parity",
        )

        assert len(calls) == 2
        assert calls[0] == calls[1]


# ---------------------------------------------------------------------------
# (b) Guardrail / middleware parity -- THE finding
# ---------------------------------------------------------------------------


class TestProviderToolCallHookParity:
    """Does a gateway slug go through the same guardrail/middleware/hook
    surface as any other tool, regardless of how the model reached it?

    Answer: NO for insertion point 1 (the tool_call bridge). model_tools.py's
    own bridge-dispatch block (~L1158-1234) resolves the underlying slug and,
    when it's a provider tool, calls dispatch_provider_tool_call and RETURNS
    immediately -- entirely before `_tool_original_args` is captured, before
    apply_tool_request_middleware, before resolve_pre_tool_block
    (pre_tool_call), before run_tool_execution_middleware, and before the
    post_tool_call / transform_tool_result hooks. None of those seams ever
    see the provider tool call.

    Insertion point 2 (the model calling the slug directly, or -- in the
    live agent loop -- agent/tool_executor.py's OWN duplicated tool_call
    unwrap substituting the slug as function_name before ever calling
    handle_function_call) goes through the full stack correctly.

    Why this matters: gateway slugs are NEVER placed directly in the
    model-visible tool list (tools/tool_search.py: assemble_tool_defs keeps
    provider tools out of `filtered_tools` entirely; they only surface via
    tool_search / tool_describe and are invoked via the `tool_call` bridge --
    see is_provider_tool_name's docstring and dispatch_tool_search/
    dispatch_provider_tool_call). So the *only* way the MODEL ever asks for
    a gateway tool is the tool_call-wrapped shape -- insertion point 1.
    model_tools.py's own `tool_call` bridge schema description
    (tools/tool_search.py `desc_call`) tells the model in so many words:
    "Policy, hooks, and approvals run exactly as for any directly-listed
    tool." For provider tools reached this way, that promise is false.

    In the current codebase this is masked in the *primary* call chain only
    because agent/tool_executor.py pre-unwraps `tool_call` itself (with a
    comment acknowledging exactly this: "the unwrap dispatches the
    underlying tool directly, so we enforce session toolset scope HERE")
    before ever calling handle_function_call -- so by the time
    handle_function_call runs, function_name is already the raw slug and
    hits insertion point 2. Any OTHER caller of the public
    handle_function_call API that does not duplicate that unwrap (a future
    ACP/batch/RL caller, or a test/tool harness that calls the public API
    as documented) gets an unguarded external-app-tool execution: no
    approval gate, no tool_request middleware (rewrite/redaction), no
    tool_execution middleware (budget/rate-limit wrappers), no
    pre_tool_call block/approve hook, no post_tool_call audit trail, no
    transform_tool_result sanitization.
    """

    def _wire_recorders(self, monkeypatch, tool_search_module):
        """Patch every hook/middleware seam handle_function_call touches,
        plus the terminal gateway dispatch, and return the shared call log
        (in call order) so both insertion points can be compared."""
        log = []

        def fake_provider_dispatch(name, arguments, *, context_id=None):
            log.append(("provider_dispatch", name))
            return json.dumps({"success": True, "context_id": context_id})

        monkeypatch.setattr(tool_search_module, "dispatch_provider_tool_call", fake_provider_dispatch)

        def fake_resolve_pre_tool_block(function_name, function_args, **kwargs):
            log.append(("pre_tool_call_hook", function_name))
            return None  # never block -- we're only measuring whether it ran

        monkeypatch.setattr(
            "hermes_cli.plugins.resolve_pre_tool_block", fake_resolve_pre_tool_block
        )

        from hermes_cli.middleware import RequestMiddlewareResult

        def fake_apply_tool_request_middleware(tool_name, args, **kwargs):
            log.append(("tool_request_middleware", tool_name))
            return RequestMiddlewareResult(payload=args, original_payload=args, trace=[])

        monkeypatch.setattr(
            "hermes_cli.middleware.apply_tool_request_middleware",
            fake_apply_tool_request_middleware,
        )

        def fake_run_tool_execution_middleware(tool_name, args, next_call, **kwargs):
            log.append(("tool_execution_middleware", tool_name))
            return next_call(args)

        monkeypatch.setattr(
            "hermes_cli.middleware.run_tool_execution_middleware",
            fake_run_tool_execution_middleware,
        )

        def fake_has_hook(hook_name):
            return True

        def fake_invoke_hook(hook_name, **kwargs):
            log.append((f"lifecycle_hook:{hook_name}", kwargs.get("tool_name")))
            return []

        monkeypatch.setattr("hermes_cli.plugins.has_hook", fake_has_hook)
        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", fake_invoke_hook)

        return log

    def test_direct_slug_call_goes_through_the_full_hook_surface(self, monkeypatch):
        """Insertion point 2: every guardrail seam fires, keyed on the real
        tool name."""
        import model_tools
        from tools import tool_search

        monkeypatch.setattr(
            tool_search, "is_provider_tool_name", lambda n: n == "GITHUB_CREATE_ISSUE"
        )
        log = self._wire_recorders(monkeypatch, tool_search)

        result = model_tools.handle_function_call(
            function_name="GITHUB_CREATE_ISSUE",
            function_args={"repo": "acme/widgets", "title": "bug"},
            context_id="ctx-1",
        )
        assert json.loads(result)["success"] is True

        stages = [entry[0] for entry in log]
        assert "pre_tool_call_hook" in stages
        assert "tool_request_middleware" in stages
        assert "tool_execution_middleware" in stages
        assert "lifecycle_hook:post_tool_call" in stages
        assert "lifecycle_hook:transform_tool_result" in stages
        assert "provider_dispatch" in stages
        # Every seam that fired was keyed on the real tool name, not "tool_call".
        for stage, name in log:
            if stage != "provider_dispatch":
                assert name in ("GITHUB_CREATE_ISSUE", None) or name == "GITHUB_CREATE_ISSUE", (
                    f"{stage} fired with tool identity {name!r}, expected the real slug"
                )

    def test_tool_call_bridge_SKIPS_the_hook_surface(self, monkeypatch):
        """HIGH: insertion point 1 (the tool_call bridge -- the only shape
        the model itself ever actually emits for a gateway tool) bypasses
        every guardrail seam. If this test starts failing because the log
        now contains hook/middleware stages, insertion point 1 has been
        fixed to route through the shared surface -- update this test to
        assert parity with test_direct_slug_call_goes_through_the_full_hook_surface
        instead of asserting the bypass.
        """
        import model_tools
        from tools import tool_search

        monkeypatch.setattr(
            tool_search, "is_provider_tool_name", lambda n: n == "GITHUB_CREATE_ISSUE"
        )
        log = self._wire_recorders(monkeypatch, tool_search)

        result = model_tools.handle_function_call(
            function_name="tool_call",
            function_args={
                "name": "GITHUB_CREATE_ISSUE",
                "arguments": {"repo": "acme/widgets", "title": "bug"},
            },
            context_id="ctx-1",
        )
        assert json.loads(result)["success"] is True

        stages = [entry[0] for entry in log]
        assert stages == ["provider_dispatch"], (
            f"expected ONLY provider_dispatch (bypass), got {stages}"
        )

    def test_bypass_is_specific_to_provider_slugs_not_ordinary_deferred_tools(self, monkeypatch):
        """Control: an ORDINARY (non-provider) deferred tool routed through
        the same tool_call bridge DOES get the full hook treatment --
        model_tools.py recurses into itself with the underlying name for
        that case (see the `else` arm at ~L1219), which is a fresh top-level
        call and hits the normal middleware/hook setup. This isolates the
        bypass to the provider-tool arm specifically, not the bridge as a
        whole.
        """
        import model_tools
        from tools import tool_search
        from tools.registry import registry

        tool_def = {
            "type": "function",
            "function": {
                "name": "some_mcp_tool",
                "description": "d",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        registry.register(
            name="some_mcp_tool",
            toolset="mcp-dispatch-parity-test",
            schema=tool_def,
            handler=lambda args, **kwargs: json.dumps({"ok": True}),
        )
        try:
            log = self._wire_recorders(monkeypatch, tool_search)
            monkeypatch.setattr(
                model_tools,
                "get_tool_definitions",
                lambda **kwargs: [tool_def],
            )

            model_tools.handle_function_call(
                function_name="tool_call",
                function_args={"name": "some_mcp_tool", "arguments": {}},
                context_id="ctx-1",
            )

            stages = [entry[0] for entry in log]
            assert "pre_tool_call_hook" in stages
            assert "tool_request_middleware" in stages
            assert "tool_execution_middleware" in stages
            assert "lifecycle_hook:post_tool_call" in stages
        finally:
            registry.deregister("some_mcp_tool")


# ---------------------------------------------------------------------------
# (c) SCREAMING_SNAKE routing heuristic, exercised through real dispatch
# ---------------------------------------------------------------------------


class TestScreamingSnakeRoutingMatrix:
    """Document actual routing for three name shapes, through the real
    handle_function_call dispatch (not just the is_provider_tool_name
    predicate in isolation -- tests/tools/test_tool_search.py already
    covers that). Flags anything that misroutes a local tool to the
    gateway or vice versa."""

    def test_local_screaming_snake_tool_routes_locally_not_to_gateway(self, monkeypatch):
        """A LOCAL tool that happens to have a SCREAMING_SNAKE name: the
        registry entry must win. It must dispatch through the normal
        registry path, never through the gateway."""
        import model_tools
        from tools import tool_search
        from tools.registry import registry

        tool_def = {
            "type": "function",
            "function": {
                "name": "LOCAL_SCREAMING_TOOL",
                "description": "d",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        registry.register(
            name="LOCAL_SCREAMING_TOOL",
            toolset="test-screaming-snake",
            schema=tool_def,
            handler=lambda args, **kwargs: json.dumps({"local": True}),
        )
        try:
            assert not tool_search.is_provider_tool_name("LOCAL_SCREAMING_TOOL")

            gateway_dispatch_calls = []
            monkeypatch.setattr(
                tool_search,
                "dispatch_provider_tool_call",
                lambda *a, **k: gateway_dispatch_calls.append((a, k)),
            )

            result = model_tools.handle_function_call(
                function_name="LOCAL_SCREAMING_TOOL",
                function_args={},
            )

            assert json.loads(result) == {"local": True}
            assert gateway_dispatch_calls == []
        finally:
            registry.deregister("LOCAL_SCREAMING_TOOL")

    def test_unknown_screaming_snake_name_routes_to_gateway(self, monkeypatch):
        """A SCREAMING_SNAKE name with NO local registry entry: the
        heuristic must treat it as a provider slug and route to the
        gateway dispatcher, never to registry.dispatch (which would just
        produce an unknown-tool error)."""
        import model_tools
        from tools import tool_search
        from tools.registry import registry

        assert registry.get_entry("UNKNOWN_SCREAMING_TOOL") is None
        assert tool_search.is_provider_tool_name("UNKNOWN_SCREAMING_TOOL")

        gateway_dispatch_calls = []

        def fake_dispatch(name, arguments, *, context_id=None):
            gateway_dispatch_calls.append((name, arguments))
            return json.dumps({"success": True})

        monkeypatch.setattr(tool_search, "dispatch_provider_tool_call", fake_dispatch)

        result = model_tools.handle_function_call(
            function_name="UNKNOWN_SCREAMING_TOOL",
            function_args={"x": 1},
        )

        assert json.loads(result)["success"] is True
        assert gateway_dispatch_calls == [("UNKNOWN_SCREAMING_TOOL", {"x": 1})]

    def test_lowercase_gateway_shaped_name_does_not_route_to_gateway(self, monkeypatch):
        """A lowercase name shaped like what a gateway slug WOULD be if it
        weren't SCREAMING_SNAKE (e.g. a model hallucinating
        'google_calendar_events_list' instead of the real
        'GOOGLECALENDAR_EVENTS_LIST' slug) must NOT be routed to the
        gateway -- it isn't a provider slug by the heuristic, and with no
        local registration it should surface as an ordinary unknown-tool
        error, not silently reach the gateway with the wrong casing."""
        import model_tools
        from tools import tool_search

        assert not tool_search.is_provider_tool_name("google_calendar_events_list")

        gateway_dispatch_calls = []
        monkeypatch.setattr(
            tool_search,
            "dispatch_provider_tool_call",
            lambda *a, **k: gateway_dispatch_calls.append((a, k)),
        )

        result = model_tools.handle_function_call(
            function_name="google_calendar_events_list",
            function_args={},
        )

        parsed = json.loads(result)
        assert "error" in parsed
        assert gateway_dispatch_calls == []
