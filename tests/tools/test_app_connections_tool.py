import json
from unittest.mock import AsyncMock, Mock

import pytest

from tools import app_connections_tool
from tools.tool_provider_gateway import (
    ProviderConnection,
    ToolProviderGatewayError,
)


@pytest.fixture
def gateway_config():
    return object()


def test_availability_requires_entitlement_and_gateway_token(monkeypatch, gateway_config):
    monkeypatch.setattr(
        "tools.tool_backend_helpers.managed_nous_tools_enabled", lambda: False
    )
    monkeypatch.setattr(
        "tools.tool_provider_gateway.resolve_tool_provider_gateway",
        lambda: gateway_config,
    )
    assert app_connections_tool.check_app_connections_available() is False

    monkeypatch.setattr(
        "tools.tool_backend_helpers.managed_nous_tools_enabled", lambda: True
    )
    monkeypatch.setattr(
        "tools.tool_provider_gateway.resolve_tool_provider_gateway", lambda: None
    )
    assert app_connections_tool.check_app_connections_available() is False

    monkeypatch.setattr(
        "tools.tool_provider_gateway.resolve_tool_provider_gateway",
        lambda: gateway_config,
    )
    assert app_connections_tool.check_app_connections_available() is True


def test_tool_is_wired_as_gated_core_tool():
    from tools.registry import registry
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS

    entry = registry.get_entry("app_connections")
    assert entry is not None
    assert entry.toolset == "app_connections"
    assert entry.check_fn is app_connections_tool.check_app_connections_available
    assert TOOLSETS["app_connections"]["tools"] == [
        "app_connections",
        "manage_tool_connections",
    ]
    assert "app_connections" in _HERMES_CORE_TOOLS


def test_status_returns_connection_shape_and_passes_no_context(monkeypatch, gateway_config):
    gateway_call = AsyncMock(
        return_value=[
            ProviderConnection(
                toolkit="gmail",
                status="connected",
                account_hint="user@example.com",
            ),
            ProviderConnection(
                toolkit="slack",
                status="not_connected",
                connect_url="https://connect.example/slack",
            ),
        ]
    )
    monkeypatch.setattr(app_connections_tool, "_resolve_gateway", lambda: gateway_config)
    monkeypatch.setattr("tools.tool_provider_gateway.connections", gateway_call)

    result = json.loads(
        app_connections_tool._dispatch_app_connections(
            {"action": "status", "toolkits": [" gmail ", "slack"]}
        )
    )

    assert result == {
        "connections": [
            {
                "toolkit": "gmail",
                "status": "connected",
                "account_hint": "user@example.com",
            },
            {
                "toolkit": "slack",
                "status": "not_connected",
                "connect_url": "https://connect.example/slack",
            },
        ]
    }
    gateway_call.assert_awaited_once_with(
        gateway_config, ["gmail", "slack"], "status", context_id=None
    )


def test_connect_opens_browser_and_returns_polling_note(monkeypatch, gateway_config):
    connect_url = "https://connect.example/gmail"
    gateway_call = AsyncMock(
        return_value=[
            ProviderConnection("gmail", "pending", connect_url=connect_url),
            ProviderConnection("github", "connected", account_hint="octocat"),
        ]
    )
    browser_open = Mock(return_value=True)
    monkeypatch.setattr(app_connections_tool, "_resolve_gateway", lambda: gateway_config)
    monkeypatch.setattr("tools.tool_provider_gateway.connections", gateway_call)
    monkeypatch.setattr("tools.mcp_oauth._can_open_browser", lambda: True)
    monkeypatch.setattr(app_connections_tool.webbrowser, "open", browser_open)

    result = json.loads(
        app_connections_tool._dispatch_app_connections(
            {"action": "connect", "toolkits": ["gmail", "github"]}
        )
    )

    browser_open.assert_called_once_with(connect_url)
    assert "status" in result["note"]
    assert "poll at most 6 times" in result["note"]
    gateway_call.assert_awaited_once_with(
        gateway_config, ["gmail", "github"], "connect", context_id=None
    )


def test_connect_headless_prints_url_without_opening_browser(
    monkeypatch, gateway_config, capsys
):
    connect_url = "https://connect.example/gmail"
    monkeypatch.setattr(app_connections_tool, "_resolve_gateway", lambda: gateway_config)
    monkeypatch.setattr(
        "tools.tool_provider_gateway.connections",
        AsyncMock(
            return_value=[
                ProviderConnection("gmail", "pending", connect_url=connect_url)
            ]
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
    assert connect_url in capsys.readouterr().err
    assert "Open the connect_url" in result["note"]


@pytest.mark.parametrize(
    "args, message",
    [
        ({"action": "delete", "toolkits": ["gmail"]}, "action must be"),
        ({"action": "status"}, "toolkits must be"),
        ({"action": "status", "toolkits": []}, "toolkits must be"),
    ],
)
def test_invalid_arguments_return_tool_error(args, message):
    result = json.loads(app_connections_tool._dispatch_app_connections(args))
    assert message in result["error"]


def test_not_entitled_returns_gateway_unavailable_error(monkeypatch):
    monkeypatch.setattr(app_connections_tool, "_resolve_gateway", lambda: None)
    monkeypatch.setattr(
        "tools.tool_backend_helpers.nous_tool_gateway_unavailable_message",
        lambda capability: f"{capability} gateway is unavailable",
    )

    result = json.loads(
        app_connections_tool._dispatch_app_connections(
            {"action": "status", "toolkits": ["gmail"]}
        )
    )

    assert "gateway is unavailable" in result["error"]


def test_gateway_error_preserves_code_and_message(monkeypatch, gateway_config):
    monkeypatch.setattr(app_connections_tool, "_resolve_gateway", lambda: gateway_config)
    monkeypatch.setattr(
        "tools.tool_provider_gateway.connections",
        AsyncMock(
            side_effect=ToolProviderGatewayError(
                "TOOLKIT_NOT_ENABLED", "Gmail is not enabled", ["gmail"]
            )
        ),
    )

    result = json.loads(
        app_connections_tool._dispatch_app_connections(
            {"action": "status", "toolkits": ["gmail"]}
        )
    )

    assert result == {
        "error": "Gmail is not enabled",
        "code": "TOOLKIT_NOT_ENABLED",
    }
