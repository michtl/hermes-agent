import json
from unittest.mock import AsyncMock

import pytest

from tools import manage_tool_connections_tool
from tools.tool_provider_gateway import ProviderConnection, ToolProviderGatewayError


@pytest.fixture
def gateway_config():
    return object()


def test_tool_is_wired_as_gated_visible_bridge_tool():
    from tools.registry import registry
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS

    entry = registry.get_entry("manage_tool_connections")

    assert entry is not None
    assert entry.toolset == "app_connections"
    assert entry.check_fn is (
        manage_tool_connections_tool.check_manage_tool_connections_available
    )
    assert "manage_tool_connections" in TOOLSETS["app_connections"]["tools"]
    assert "manage_tool_connections" in _HERMES_CORE_TOOLS


def test_mixed_connections_preserve_link_and_notes(monkeypatch, gateway_config):
    connect_url = "https://connect.example/link/lk_short_lived"
    gateway_call = AsyncMock(
        return_value=[
            ProviderConnection(
                "googlecalendar",
                "pending",
                connect_url=connect_url,
                note="Open the link to finish authorizing.",
            ),
            ProviderConnection(
                "hackernews",
                "connected",
                note="Connected and ready to use.",
            ),
        ]
    )
    monkeypatch.setattr(
        manage_tool_connections_tool, "_resolve_gateway", lambda: gateway_config
    )
    monkeypatch.setattr("tools.tool_provider_gateway.connections", gateway_call)

    raw_result = manage_tool_connections_tool._dispatch_manage_tool_connections(
        {"toolkits": [" googlecalendar ", "hackernews"]}
    )
    result = json.loads(raw_result)

    assert result == {
        "connections": [
            {
                "toolkit": "googlecalendar",
                "status": "pending",
                "connect_url": connect_url,
                "note": "Open the link to finish authorizing.",
            },
            {
                "toolkit": "hackernews",
                "status": "connected",
                "note": "Connected and ready to use.",
            },
        ]
    }
    assert connect_url in raw_result
    gateway_call.assert_awaited_once_with(
        gateway_config,
        ["googlecalendar", "hackernews"],
        "manage",
        context_id=None,
        reinitiate=False,
    )


@pytest.mark.parametrize("args", [{}, {"toolkits": []}])
def test_omitted_or_empty_toolkits_survey_everything(
    monkeypatch, gateway_config, args
):
    gateway_call = AsyncMock(return_value=[])
    monkeypatch.setattr(
        manage_tool_connections_tool, "_resolve_gateway", lambda: gateway_config
    )
    monkeypatch.setattr("tools.tool_provider_gateway.connections", gateway_call)

    result = json.loads(
        manage_tool_connections_tool._dispatch_manage_tool_connections(args)
    )

    assert result == {"connections": []}
    gateway_call.assert_awaited_once_with(
        gateway_config, [], "manage", context_id=None, reinitiate=False
    )


def test_gateway_error_is_clean_and_vendor_neutral(monkeypatch, gateway_config):
    monkeypatch.setattr(
        manage_tool_connections_tool, "_resolve_gateway", lambda: gateway_config
    )
    monkeypatch.setattr(
        "tools.tool_provider_gateway.connections",
        AsyncMock(
            side_effect=ToolProviderGatewayError(
                "CONNECTION_FAILED", "Composio could not create the connection"
            )
        ),
    )

    raw_result = manage_tool_connections_tool._dispatch_manage_tool_connections(
        {"toolkits": ["googlecalendar"], "reinitiate": True}
    )
    result = json.loads(raw_result)

    assert result == {
        "error": "connection service could not create the connection",
        "code": "CONNECTION_FAILED",
    }
    assert "composio" not in raw_result.lower()
    assert "Traceback" not in raw_result
