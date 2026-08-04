"""DELEG-4 adversarial re-derivation (bridge half, in-process).

Proves deterministically that when /v1/execute returns a per-tool result with
successful=false and NO error string (what the LIVE gateway does for a slug that
does not exist), tools.tool_search.dispatch_provider_tool_call surfaces the
untyped placeholder "the tool call failed" to the model.

Run:
  cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge && \
    TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 .venv/bin/python -m pytest \
    tests/integration_tool_provider/adv_deleg4/test_unknown_slug_execute_error.py -q
"""
import json
from unittest.mock import patch

import tools.tool_search as ts
from tools.tool_provider_gateway import ProviderExecuteResponse, ProviderExecuteResult


def _dispatch(result: ProviderExecuteResult) -> dict:
    response = ProviderExecuteResponse(context_id="trs_TEST", results=[result])
    with patch.object(ts, "_tool_provider_gateway_config", return_value=object()), \
         patch("tools.tool_provider_gateway.execute", return_value=response) as gw:
        gw.__name__ = "execute"
        with patch("model_tools._run_async", side_effect=lambda coro: response):
            return json.loads(ts.dispatch_provider_tool_call("HACKERNEWS_GET_FRONTPAGE", {}))


def test_unknown_slug_shape_yields_untyped_placeholder():
    payload = _dispatch(ProviderExecuteResult(slug="HACKERNEWS_GET_FRONTPAGE", successful=False))
    assert payload["success"] is False
    assert payload["error"] == "the tool call failed"
    assert "code" not in payload


def test_real_tool_level_error_is_passed_through_verbatim():
    payload = _dispatch(ProviderExecuteResult(
        slug="HACKERNEWS_SEARCH_POSTS",
        successful=False,
        error="Invalid request data provided\n- Following fields are missing: {'query'}",
    ))
    assert payload["error"].startswith("Invalid request data provided")
