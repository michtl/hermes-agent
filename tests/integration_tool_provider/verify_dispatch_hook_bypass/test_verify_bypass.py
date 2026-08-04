"""ADVERSARIAL RE-DERIVATION of finding dispatch-hook-bypass-1.

Written from scratch (does not import or reuse the finder's harness under
tests/integration_tool_provider/dispatch/). Patches the five guardrail seams at
their DEFINING modules (hermes_cli.middleware / hermes_cli.plugins), because
model_tools.handle_function_call imports them lazily inside the function body --
so a source-module patch is the only one that takes effect.

Run:
  cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge && \
  TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 .venv/bin/python -m pytest \
  tests/integration_tool_provider/verify_dispatch_hook_bypass/test_verify_bypass.py -q -v
"""

import json
from contextlib import ExitStack
from unittest.mock import patch

import pytest

import model_tools
from tools import tool_search as ts

SLUG = "GITHUB_CREATE_ISSUE"
ARGS = {"owner": "o", "repo": "r", "title": "t"}


class _MwResult:
    """Duck-type of whatever apply_tool_request_middleware returns."""

    def __init__(self, payload):
        self.payload = payload
        self.original_payload = dict(payload)
        self.trace = []


def _run(function_name, function_args):
    """Invoke handle_function_call with every seam instrumented; return stages."""
    stages = []

    def fake_request_mw(name, args, **kw):
        stages.append("tool_request_middleware")
        return _MwResult(args)

    def fake_pre_block(name, args, **kw):
        stages.append("pre_tool_call_hook")
        return None  # do not block

    def fake_exec_mw(name, args, dispatch, **kw):
        stages.append("tool_execution_middleware")
        return dispatch(args)

    def fake_post(**kw):
        stages.append("post_tool_call_hook")

    def fake_has_hook(name):
        if name == "transform_tool_result":
            stages.append("transform_tool_result_checked")
            return False
        return False

    def fake_provider_dispatch(name, args, **kw):
        stages.append("PROVIDER_DISPATCH")
        return json.dumps({"ok": True, "slug": name})

    with ExitStack() as st:
        st.enter_context(patch("hermes_cli.middleware.apply_tool_request_middleware", fake_request_mw))
        st.enter_context(patch("hermes_cli.middleware.run_tool_execution_middleware", fake_exec_mw))
        st.enter_context(patch("hermes_cli.plugins.resolve_pre_tool_block", fake_pre_block))
        st.enter_context(patch.object(model_tools, "_emit_post_tool_call_hook", fake_post))
        st.enter_context(patch("hermes_cli.plugins.has_hook", fake_has_hook))
        st.enter_context(patch.object(ts, "dispatch_provider_tool_call", fake_provider_dispatch))
        model_tools.handle_function_call(
            function_name=function_name,
            function_args=function_args,
            session_id="s1",
            tool_call_id="tc1",
        )
    return stages


def test_slug_is_actually_classified_as_provider():
    """Guard: the whole finding is vacuous if the heuristic doesn't fire."""
    assert ts.is_provider_tool_name(SLUG) is True


def test_direct_slug_traverses_the_full_guardrail_stack():
    stages = _run(SLUG, dict(ARGS))
    print("\nDIRECT-NAME stages:", stages)
    assert "PROVIDER_DISPATCH" in stages, stages
    for seam in ("tool_request_middleware", "pre_tool_call_hook",
                 "tool_execution_middleware", "post_tool_call_hook"):
        assert seam in stages, f"missing {seam}: {stages}"


def test_tool_call_bridge_skips_the_entire_guardrail_stack():
    stages = _run("tool_call", {"name": SLUG, "arguments": dict(ARGS)})
    print("\nBRIDGE stages:", stages)
    assert "PROVIDER_DISPATCH" in stages, stages
    assert stages == ["PROVIDER_DISPATCH"], (
        f"expected ONLY the terminal dispatch, got {stages}"
    )


def test_control_ordinary_deferred_tool_via_bridge_keeps_its_guardrails():
    """Isolates the bypass to the provider arm: a non-provider deferrable
    tool routed through the identical bridge still gets the full stack."""
    ordinary = None
    try:
        defs = model_tools.get_tool_definitions(quiet_mode=True,
                                                skip_tool_search_assembly=True) or []
    except Exception:
        defs = []
    for td in defs:
        nm = (td.get("function") or {}).get("name", "")
        if nm and ts.is_deferrable_tool_name(nm):
            ordinary = nm
            break
    if not ordinary:
        pytest.skip("no ordinary deferrable tool available in this registry")

    stages = []

    def fake_request_mw(name, args, **kw):
        stages.append(f"tool_request_middleware:{name}")
        return _MwResult(args)

    def fake_pre_block(name, args, **kw):
        stages.append(f"pre_tool_call_hook:{name}")
        return "BLOCKED-BY-PROBE"  # block so we never really execute anything

    with ExitStack() as st:
        st.enter_context(patch("hermes_cli.middleware.apply_tool_request_middleware", fake_request_mw))
        st.enter_context(patch("hermes_cli.plugins.resolve_pre_tool_block", fake_pre_block))
        st.enter_context(patch.object(model_tools, "_emit_post_tool_call_hook", lambda **kw: None))
        # Admit the tool through the bridge's scope gate and skip arg probing,
        # so we isolate the HOOK question rather than the scoping question.
        st.enter_context(patch.object(ts, "scoped_deferrable_names",
                                      lambda defs: frozenset({ordinary})))
        st.enter_context(patch.object(ts, "validate_deferred_call_args",
                                      lambda name, args: None))
        model_tools.handle_function_call(
            function_name="tool_call",
            function_args={"name": ordinary, "arguments": {}},
            session_id="s1", tool_call_id="tc1",
        )
    print(f"\nCONTROL ({ordinary}) stages:", stages)
    assert any(s.startswith("pre_tool_call_hook") for s in stages), stages
    assert any(ordinary in s for s in stages), stages


def test_provider_tools_are_absent_from_the_model_visible_tool_array():
    """The reachability premise: if provider slugs were directly listed, the
    model could reach the guarded arm and the bypass would be unreachable."""
    defs = model_tools.get_tool_definitions(quiet_mode=True) or []
    names = [(td.get("function") or {}).get("name", "") for td in defs]
    provider_visible = [n for n in names if ts.is_provider_tool_name(n)]
    print("\nvisible provider slugs:", provider_visible)
    print("bridge tools present:", [n for n in names if n in ts.BRIDGE_TOOL_NAMES])
    assert provider_visible == [], provider_visible


def test_model_visible_promise_text_is_the_one_being_violated():
    """Pull the promise out of the REAL assembled model-facing tool array."""
    defs = model_tools.get_tool_definitions(quiet_mode=True) or []
    desc = ""
    for d in defs:
        fn = d.get("function") or {}
        if fn.get("name") == ts.TOOL_CALL_NAME:
            desc = fn.get("description") or ""
    print("\nLIVE tool_call description:", desc)
    assert "Policy, hooks, and approvals run exactly as for any directly-listed tool." in desc
