"""Lens: app_connections status/connect shapes + bounded-polling proof.

Fast probes only (mocked gateway, no network) per the SPEED MANDATE. Covers:

  - status is side-effect-free (single gateway call, correct shape out).
  - connect returns a connect_url and a headless-safe browser-open path.
  - a NEVER-CONNECTING backend (always "pending") cannot make a single
    app_connections call block or loop internally — the poll cadence named
    in the tool's returned "note" (_POLL_MAX_ATTEMPTS / _POLL_INTERVAL_SECONDS)
    is advisory text aimed at the calling model's own tool-call loop, NOT an
    internal retry/sleep inside _dispatch_app_connections. This file proves
    that distinction concretely: one dispatch == one gateway round-trip,
    always, regardless of the returned connection status, and no sleep/wait
    primitive is ever invoked by the tool itself.
  - no vendor name ever appears in the JSON the model/user sees.

Run:
    cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge && \
    TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 .venv/bin/python -m pytest \
    tests/integration_tool_provider/connections/test_app_connections_bounded.py -q
"""

import json
import time
from unittest.mock import AsyncMock, Mock

import pytest

from tools import app_connections_tool
from tools.tool_provider_gateway import ProviderConnection, TOOL_PROVIDER_VENDOR

# Scan pattern only — the vendor's product name must never appear in any
# model-visible or user-visible string produced by the tool. TOOL_PROVIDER_VENDOR
# itself is just the gateway's internal path segment ("tools"), not the vendor
# name, so it is not a useful scan target; the real target is the vendor's
# actual product name, checked for separately below by literal.
_VENDOR_LITERAL = "composio"


@pytest.fixture
def gateway_config():
    return object()


def test_status_is_single_side_effect_free_call(monkeypatch, gateway_config):
    """action='status' makes exactly one gateway call and returns cleanly."""
    gateway_call = AsyncMock(
        return_value=[
            ProviderConnection(toolkit="gmail", status="connected", account_hint="a@b.com"),
            ProviderConnection(toolkit="slack", status="not_connected"),
        ]
    )
    monkeypatch.setattr(app_connections_tool, "_resolve_gateway", lambda: gateway_config)
    monkeypatch.setattr("tools.tool_provider_gateway.connections", gateway_call)

    result = json.loads(
        app_connections_tool._dispatch_app_connections(
            {"action": "status", "toolkits": ["gmail", "slack"]}
        )
    )

    assert gateway_call.await_count == 1
    gateway_call.assert_awaited_once_with(
        gateway_config, ["gmail", "slack"], "status", context_id=None
    )
    assert result == {
        "connections": [
            {"toolkit": "gmail", "status": "connected", "account_hint": "a@b.com"},
            {"toolkit": "slack", "status": "not_connected"},
        ]
    }


def test_connect_headless_never_opens_browser_and_prints_url(
    monkeypatch, gateway_config, capsys
):
    connect_url = "https://connect.example/gmail"
    monkeypatch.setattr(app_connections_tool, "_resolve_gateway", lambda: gateway_config)
    monkeypatch.setattr(
        "tools.tool_provider_gateway.connections",
        AsyncMock(
            return_value=[ProviderConnection("gmail", "pending", connect_url=connect_url)]
        ),
    )
    monkeypatch.setattr("tools.mcp_oauth._can_open_browser", lambda: False)
    browser_open = Mock()
    monkeypatch.setattr(app_connections_tool.webbrowser, "open", browser_open)

    result = json.loads(
        app_connections_tool._dispatch_app_connections(
            {"action": "connect", "toolkits": ["gmail"]}
        )
    )

    browser_open.assert_not_called()
    err = capsys.readouterr().err
    assert connect_url in err
    assert "Headless environment detected" in err
    assert result["connections"][0]["connect_url"] == connect_url
    assert "Open the connect_url" in result["note"]


def test_never_connecting_backend_does_not_block_or_loop_internally(
    monkeypatch, gateway_config
):
    """A backend that ALWAYS reports 'pending' must not hang a single call.

    Proves the bound the hard way: patch every sleep/wait primitive the tool
    module could plausibly reach to raise if called, then invoke the
    dispatch several times in a row exactly as an (adversarial or confused)
    calling model might if it ignored the poll-cadence guidance in the
    returned note. Each call must still be a single immediate gateway
    round-trip with no internal wait.
    """

    def _forbidden_sleep(*_a, **_kw):
        raise AssertionError(
            "app_connections dispatch must never sleep/wait internally — "
            "polling cadence is advisory text for the calling model, not an "
            "internal loop"
        )

    monkeypatch.setattr(time, "sleep", _forbidden_sleep)

    never_connects = AsyncMock(
        return_value=[ProviderConnection("gmail", "pending", connect_url="https://x/y")]
    )
    monkeypatch.setattr(app_connections_tool, "_resolve_gateway", lambda: gateway_config)
    monkeypatch.setattr("tools.tool_provider_gateway.connections", never_connects)
    monkeypatch.setattr("tools.mcp_oauth._can_open_browser", lambda: False)
    monkeypatch.setattr(app_connections_tool.webbrowser, "open", Mock())

    attempts = app_connections_tool._POLL_MAX_ATTEMPTS + 3  # deliberately over-poll
    start = time.monotonic()
    for _ in range(attempts):
        result = json.loads(
            app_connections_tool._dispatch_app_connections(
                {"action": "connect", "toolkits": ["gmail"]}
            )
        )
        assert result["connections"][0]["status"] == "pending"
    elapsed = time.monotonic() - start

    # No internal backoff exists, so `attempts` calls to a mocked backend
    # complete near-instantly. A generous bound (2s) still proves there is
    # no per-call sleep anywhere near the documented 10s poll interval.
    assert elapsed < 2.0, (
        f"{attempts} dispatch calls took {elapsed:.3f}s — something is "
        "sleeping/blocking inside app_connections dispatch"
    )
    assert never_connects.await_count == attempts  # exactly 1:1, no internal retry


def test_poll_cadence_is_advisory_text_not_an_enforced_loop(monkeypatch, gateway_config):
    """Pin today's advisory numbers so a change here is a deliberate diff."""
    assert app_connections_tool._POLL_MAX_ATTEMPTS == 6
    assert app_connections_tool._POLL_INTERVAL_SECONDS == 10

    monkeypatch.setattr(app_connections_tool, "_resolve_gateway", lambda: gateway_config)
    monkeypatch.setattr(
        "tools.tool_provider_gateway.connections",
        AsyncMock(
            return_value=[ProviderConnection("gmail", "pending", connect_url="https://x/y")]
        ),
    )
    monkeypatch.setattr("tools.mcp_oauth._can_open_browser", lambda: False)
    monkeypatch.setattr(app_connections_tool.webbrowser, "open", Mock())

    result = json.loads(
        app_connections_tool._dispatch_app_connections(
            {"action": "connect", "toolkits": ["gmail"]}
        )
    )
    assert "poll at most 6 times, about 10 seconds" in result["note"]


@pytest.mark.parametrize("action", ["status", "connect"])
def test_no_vendor_name_in_status_or_connect_output(monkeypatch, gateway_config, action):
    monkeypatch.setattr(app_connections_tool, "_resolve_gateway", lambda: gateway_config)
    monkeypatch.setattr(
        "tools.tool_provider_gateway.connections",
        AsyncMock(
            return_value=[
                ProviderConnection("gmail", "pending", connect_url="https://connect.example/gmail"),
                ProviderConnection("github", "connected", account_hint="octocat"),
            ]
        ),
    )
    monkeypatch.setattr("tools.mcp_oauth._can_open_browser", lambda: False)
    monkeypatch.setattr(app_connections_tool.webbrowser, "open", Mock())

    raw = app_connections_tool._dispatch_app_connections(
        {"action": action, "toolkits": ["gmail", "github"]}
    )
    assert _VENDOR_LITERAL not in raw.lower()


def test_gateway_unavailable_message_has_no_vendor_name(monkeypatch):
    monkeypatch.setattr(app_connections_tool, "_resolve_gateway", lambda: None)
    result = json.loads(
        app_connections_tool._dispatch_app_connections(
            {"action": "status", "toolkits": ["gmail"]}
        )
    )
    assert "error" in result
    assert _VENDOR_LITERAL not in result["error"].lower()
