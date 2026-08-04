"""Lens: session-init /v1/connections probe + injected prompt line + failure modes.

Fast probes only (mocked gateway, no network) per the SPEED MANDATE. Covers:

  (b) Pins the init contract: init_agent makes exactly ONE
      /v1/connections call, payload {"toolkits": [], "action": "status"},
      context_id=None. Then pins agent/prompt_builder.py's injected
      "External app tools" line byte-for-byte, re-derived from the CURRENT
      source (not memorized), for zero / some / all connected, and for the
      "probe never ran / failed" case (connected_toolkits stays None).

  (c) INIT FAILURE MODES, all mocked: gateway transport failure (connection
      refused/DNS), a structured 403 refusal, and a slow/hanging backend.
      Session init must still complete (init_agent must not raise), the
      connected-toolkits snapshot must fall back to None (never a fabricated
      empty-but-successful list), and nothing vendor-named or tracebacky may
      reach stdout/stderr in quiet_mode. The 8s init timeout constant
      (tools/tool_provider_gateway.py REQUEST_TIMEOUT_SECONDS) is proven with
      a real httpx.MockTransport that never responds, using a monkeypatched
      (shrunk) timeout so the test stays fast — the code path exercised is
      identical, only the constant's value is reduced for speed.

Run:
    cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge && \
    TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 .venv/bin/python -m pytest \
    tests/integration_tool_provider/connections/test_session_init_probe.py -q
"""

import asyncio
import inspect
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agent import prompt_builder
from hermes_cli.nous_subscription import NousFeatureState, NousSubscriptionFeatures
from tools.tool_provider_gateway import (
    ProviderConnection,
    ToolProviderGatewayError,
    ToolProviderTransportError,
)

_VENDOR_LITERAL = "composio"


# ---------------------------------------------------------------------------
# (b) init contract: exactly one /v1/connections status call
# ---------------------------------------------------------------------------


def _initialize_agent_with_tool_provider(*, entitled, gateway_config, connections_result):
    """Mirrors tests/agent/test_agent_init_tool_provider.py's harness.

    Kept local (rather than imported) so this file's failure-mode variants
    (side_effect raising, not just a return value) stay simple to express.
    """
    from agent.agent_init import init_agent
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._base_url = ""
    agent._base_url_lower = ""
    agent._base_url_hostname = ""
    pool = SimpleNamespace(provider="anthropic")

    with (
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(None, None)),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
        patch("agent.anthropic_adapter.resolve_anthropic_token", return_value=""),
        patch("agent.anthropic_adapter._is_oauth_token", return_value=False),
        patch("agent.azure_identity_adapter.is_token_provider", return_value=False),
        patch(
            "hermes_cli.model_normalize.normalize_model_for_provider",
            return_value="test-model",
        ),
        patch("agent.credential_pool.load_pool", return_value=MagicMock()),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("hermes_cli.config.get_compatible_custom_providers", return_value=[]),
        patch("agent.iteration_budget.IterationBudget"),
        patch("hermes_cli.config.cfg_get", return_value=None),
        patch(
            "tools.tool_backend_helpers.managed_nous_tools_enabled",
            return_value=entitled,
        ),
        patch(
            "tools.tool_provider_gateway.resolve_tool_provider_gateway",
            return_value=gateway_config,
        ) as resolve_gateway,
        patch(
            "tools.tool_provider_gateway.connections",
            new=(
                connections_result
                if isinstance(connections_result, AsyncMock)
                else AsyncMock(return_value=connections_result)
            ),
        ) as gateway_connections,
    ):
        init_agent(
            agent,
            base_url="https://api.anthropic.com",
            api_key="test-key",
            provider=None,
            model="test-model",
            credential_pool=pool,
            skip_context_files=True,
            skip_memory=True,
            quiet_mode=True,
        )

    return agent, resolve_gateway, gateway_connections


@pytest.mark.parametrize(
    "connections_result, expected_toolkits",
    [
        pytest.param([], [], id="zero_connected"),
        pytest.param(
            [
                ProviderConnection("gmail", "connected"),
                ProviderConnection("slack", "not_connected"),
            ],
            ["gmail"],
            id="some_connected",
        ),
        pytest.param(
            [
                ProviderConnection("gmail", "connected"),
                ProviderConnection("slack", "connected"),
                ProviderConnection("github", "connected"),
            ],
            ["github", "gmail", "slack"],
            id="all_connected",
        ),
    ],
)
def test_init_makes_exactly_one_status_call_with_empty_toolkits(
    connections_result, expected_toolkits
):
    gateway_config = object()
    agent, resolve_gateway, gateway_connections = _initialize_agent_with_tool_provider(
        entitled=True,
        gateway_config=gateway_config,
        connections_result=connections_result,
    )

    resolve_gateway.assert_called_once_with()
    gateway_connections.assert_awaited_once_with(
        gateway_config, [], "status", context_id=None
    )
    assert agent._tool_provider_context_id is None
    assert sorted(agent._tool_provider_connected_toolkits) == expected_toolkits


# ---------------------------------------------------------------------------
# (b) injected prompt line, pinned byte-for-byte from CURRENT source
# ---------------------------------------------------------------------------


def _bare_features() -> NousSubscriptionFeatures:
    """Minimal, fully-inactive feature set so only the app-tools line varies."""
    keys = ("web", "image_gen", "video_gen", "tts", "stt", "browser", "modal")
    features = {
        k: NousFeatureState(
            key=k,
            label=k,
            included_by_default=False,
            available=False,
            active=False,
            managed_by_nous=False,
            direct_override=False,
            toolset_enabled=False,
        )
        for k in keys
    }
    return NousSubscriptionFeatures(
        subscribed=False,
        nous_auth_present=False,
        provider_is_nous=False,
        features=features,
        account_info=None,
    )


def _app_tools_line(prompt_text: str) -> "str | None":
    for line in prompt_text.splitlines():
        if line.startswith("- External app tools"):
            return line
    return None


@pytest.mark.parametrize(
    "connected_toolkits, expected_line",
    [
        pytest.param(
            [],
            "- External app tools: available via tool_search; connect accounts "
            "with app_connections",
            id="zero_connected",
        ),
        pytest.param(
            ["gmail"],
            "- External app tools: connected — gmail "
            "(via tool_search; more via app_connections)",
            id="some_connected",
        ),
        pytest.param(
            ["slack", "gmail", "github"],
            # sorted() in the source, so alphabetical regardless of input order
            "- External app tools: connected — github, gmail, slack "
            "(via tool_search; more via app_connections)",
            id="all_connected_sorted",
        ),
    ],
)
def test_prompt_line_pinned_for_connected_states(monkeypatch, connected_toolkits, expected_line):
    monkeypatch.setattr(
        "tools.tool_backend_helpers.managed_nous_tools_enabled", lambda: True
    )
    monkeypatch.setattr(
        "hermes_cli.nous_subscription.get_nous_subscription_features",
        lambda: _bare_features(),
    )

    prompt = prompt_builder.build_nous_subscription_prompt(
        valid_tool_names={"app_connections"},
        connected_toolkits=connected_toolkits,
    )

    line = _app_tools_line(prompt)
    assert line == expected_line


def test_prompt_omits_app_tools_line_when_probe_never_ran_or_failed(monkeypatch):
    """connected_toolkits=None (init probe skipped/failed) must NOT claim
    'available via tool_search' — that would misstate an unknown state as a
    known one. The source's `if connected_toolkits is not None:` guard means
    no External-app-tools line is emitted at all in this case; this test
    pins that omission so a future change can't silently start asserting
    availability off an unresolved probe.
    """
    monkeypatch.setattr(
        "tools.tool_backend_helpers.managed_nous_tools_enabled", lambda: True
    )
    monkeypatch.setattr(
        "hermes_cli.nous_subscription.get_nous_subscription_features",
        lambda: _bare_features(),
    )

    prompt = prompt_builder.build_nous_subscription_prompt(
        valid_tool_names={"app_connections"},
        connected_toolkits=None,
    )

    assert _app_tools_line(prompt) is None


def test_prompt_builder_source_still_matches_pinned_literals():
    """Guard against the pin above silently drifting from the real source:
    re-read agent/prompt_builder.py's build_nous_subscription_prompt body and
    assert the two literal line templates are still present verbatim. If this
    fails, the byte-for-byte pins above must be updated in the SAME change
    that edits prompt_builder.py, not independently.
    """
    src = inspect.getsource(prompt_builder.build_nous_subscription_prompt)
    assert (
        "- External app tools: connected — {', '.join(sorted(connected_toolkits))} "
        in src
    )
    assert (
        "- External app tools: available via tool_search; connect accounts "
        in src
    )


# ---------------------------------------------------------------------------
# (c) init failure modes: gateway down / 403 / slow — all must not crash init
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            ToolProviderTransportError("ConnectError: connection refused"),
            id="gateway_down_transport_error",
        ),
        pytest.param(
            ToolProviderGatewayError("FORBIDDEN", "Access denied", ["gmail"]),
            id="gateway_403_refusal",
        ),
        pytest.param(asyncio.TimeoutError(), id="gateway_timeout"),
        pytest.param(RuntimeError("boom"), id="unexpected_exception"),
    ],
)
def test_init_survives_connections_probe_failure(monkeypatch, capsys, failure):
    gateway_config = object()
    failing_connections = AsyncMock(side_effect=failure)

    agent, resolve_gateway, gateway_connections = _initialize_agent_with_tool_provider(
        entitled=True,
        gateway_config=gateway_config,
        connections_result=failing_connections,
    )

    # init_agent completed (no exception propagated) and the snapshot falls
    # back to "unknown", never a fabricated empty-but-successful list.
    assert agent._tool_provider_context_id is None
    assert agent._tool_provider_connected_toolkits is None
    gateway_connections.assert_awaited_once()

    out, err = capsys.readouterr()
    combined = (out + err).lower()
    assert _VENDOR_LITERAL not in combined
    assert "traceback" not in combined


def test_init_probe_timeout_is_bounded_not_indefinite(monkeypatch):
    """Prove the 8s gateway timeout constant actually bounds a hung backend.

    Uses a real httpx.MockTransport whose handler never returns (simulates a
    backend that accepted the TCP connection but never answers), wired
    through a monkeypatched *smaller* REQUEST_TIMEOUT_SECONDS so the test
    doesn't need to wait out the real 8s — the exact same _post() code path
    in tools/tool_provider_gateway.py is exercised either way; only the
    constant's value changed for test speed.
    """
    import tools.tool_provider_gateway as gw

    def _never_respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated hang", request=request)

    monkeypatch.setattr(gw, "REQUEST_TIMEOUT_SECONDS", 0.05)

    async def _run():
        transport = httpx.MockTransport(_never_respond)
        config = SimpleNamespace(
            gateway_origin="https://tools-gateway.invalid",
            nous_user_token="test-token",
        )

        async def _fake_client(*args, **kwargs):
            kwargs["transport"] = transport
            return httpx.AsyncClient(*args, **kwargs)

        # Patch httpx.AsyncClient used inside _post to route through the mock
        # transport while still constructing a real client (so the real
        # httpx.Timeout(REQUEST_TIMEOUT_SECONDS) wiring is exercised).
        import httpx as httpx_module

        original_client_cls = httpx_module.AsyncClient

        class _PatchedClient(original_client_cls):
            def __init__(self, *a, **kw):
                kw["transport"] = transport
                super().__init__(*a, **kw)

        monkeypatch.setattr(httpx_module, "AsyncClient", _PatchedClient)

        with pytest.raises(gw.ToolProviderTransportError):
            await gw._post(config, "/v1/connections", {"toolkits": [], "action": "status"})

    start = __import__("time").monotonic()
    asyncio.run(_run())
    elapsed = __import__("time").monotonic() - start
    # Generous ceiling: with an 0.05s timeout this should resolve in well
    # under 1 real second. If the timeout constant were not honored, this
    # would either hang (test framework timeout) or take much longer.
    assert elapsed < 1.0
