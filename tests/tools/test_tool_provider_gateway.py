import asyncio
import sys
import types
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import httpx
import pytest


TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"


def _load_tool_module(module_name: str, filename: str):
    spec = spec_from_file_location(module_name, TOOLS_DIR / filename)
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# These modules are loaded by path under a synthetic `tools` package, to avoid importing the real
# package's heavy dependency graph. That requires temporarily owning shared sys.modules names, which
# other test files also import — so the entries are restored IMMEDIATELY after loading rather than
# in a teardown fixture. A fixture would be far too late: pytest imports every test module during
# collection, before any test runs, so the substitutions would still be in place while a sibling
# file's tests execute, and that file would monkeypatch these stubs instead of the real modules.
#
# Restoring straight away is safe because the loaded module objects are already bound to this
# file's globals, and tool_provider_gateway resolves its own `from tools.managed_tool_gateway ...`
# import at exec time (its only runtime import is httpx).
# The snapshot covers every `tools*` entry rather than just the two loaded below, because executing
# them pulls in further submodules transitively (tools.tool_backend_helpers, for one) which land in
# sys.modules under the synthetic package and would otherwise leak just as badly.
_ORIGINAL_MODULES = {
    name: module for name, module in sys.modules.items() if name == "tools" or name.startswith("tools.")
}

tools_package = types.ModuleType("tools")
tools_package.__path__ = [str(TOOLS_DIR)]
sys.modules["tools"] = tools_package
managed_tool_gateway = _load_tool_module(
    "tools.managed_tool_gateway",
    "managed_tool_gateway.py",
)
tool_provider_gateway = _load_tool_module(
    "tools.tool_provider_gateway",
    "tool_provider_gateway.py",
)

for _name in [name for name in sys.modules if name == "tools" or name.startswith("tools.")]:
    if _name in _ORIGINAL_MODULES:
        sys.modules[_name] = _ORIGINAL_MODULES[_name]
    else:
        del sys.modules[_name]


def _config(origin: str = "https://tools-gateway.example.com"):
    return managed_tool_gateway.ManagedToolGatewayConfig(
        vendor="tools",
        gateway_origin=origin,
        nous_user_token="nous-token",
        managed_mode=True,
    )


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.body = body

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def _fake_http(monkeypatch, *, responses=None, error=None):
    calls = []
    queued = iter(responses or [])

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url, headers=None, json=None):
            calls.append({"url": url, "headers": headers, "json": json})
            if error is not None:
                raise error
            return next(queued)

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    return calls


def test_resolve_tool_provider_gateway_entitlement_origin_and_override(monkeypatch):
    monkeypatch.setattr(managed_tool_gateway, "managed_nous_tools_enabled", lambda: True)
    monkeypatch.setattr(managed_tool_gateway, "read_nous_access_token", lambda: "nous-token")
    monkeypatch.setenv("TOOL_GATEWAY_DOMAIN", "example.test")
    monkeypatch.delenv("TOOLS_GATEWAY_URL", raising=False)

    resolved = tool_provider_gateway.resolve_tool_provider_gateway()

    assert resolved is not None
    assert resolved.gateway_origin == "https://tools-gateway.example.test"
    assert resolved.vendor == "tools"
    assert tool_provider_gateway.is_tool_provider_entitled() is True

    monkeypatch.setenv("TOOLS_GATEWAY_URL", "http://tools-gateway.localhost:3009/")
    overridden = tool_provider_gateway.resolve_tool_provider_gateway()
    assert overridden is not None
    assert overridden.gateway_origin == "http://tools-gateway.localhost:3009"


def test_resolve_tool_provider_gateway_requires_entitlement_and_token(monkeypatch):
    monkeypatch.setattr(managed_tool_gateway, "managed_nous_tools_enabled", lambda: False)
    monkeypatch.setattr(managed_tool_gateway, "read_nous_access_token", lambda: "nous-token")
    assert tool_provider_gateway.resolve_tool_provider_gateway() is None
    assert tool_provider_gateway.is_tool_provider_entitled() is False

    monkeypatch.setattr(managed_tool_gateway, "managed_nous_tools_enabled", lambda: True)
    monkeypatch.setattr(managed_tool_gateway, "read_nous_access_token", lambda: None)
    assert tool_provider_gateway.resolve_tool_provider_gateway() is None


def test_search_parses_groups_tools_connections_and_guidance(monkeypatch):
    calls = _fake_http(
        monkeypatch,
        responses=[FakeResponse(body={
            "context_id": "ctx-1",
            "results": [
                {
                    "use_case": "Read mail",
                    "tools": [{
                        "slug": "GMAIL_FETCH_EMAILS",
                        "toolkit": "gmail",
                        "description": "Fetch emails",
                        "connected": True,
                        "input_schema": {"type": "object"},
                    }],
                    "plan": ["Fetch matching messages"],
                },
                {
                    "use_case": "Create issue",
                    "tools": [{
                        "slug": "GITHUB_CREATE_ISSUE",
                        "toolkit": "github",
                        "description": "Create an issue",
                        "connected": False,
                    }],
                    "pitfalls": ["Connect GitHub first"],
                },
            ],
            "connections": [
                {"toolkit": "gmail", "connected": True},
                {"toolkit": "github", "connected": False},
            ],
            "guidance": ["Use the returned context for follow-ups"],
        })],
    )

    result = asyncio.run(tool_provider_gateway.search(
        _config(),
        ["read recent mail", "create a GitHub issue"],
        context_id="ctx-old",
        model="hermes-4",
    ))

    assert result.context_id == "ctx-1"
    assert len(result.results) == 2
    assert [tool.slug for tool in result.all_tools()] == [
        "GMAIL_FETCH_EMAILS",
        "GITHUB_CREATE_ISSUE",
    ]
    assert result.all_tools()[0].input_schema == {"type": "object"}
    assert result.all_tools()[0].connected is True
    assert result.all_tools()[1].toolkit == "github"
    assert result.connections[1] == {"toolkit": "github", "connected": False}
    assert result.guidance == ["Use the returned context for follow-ups"]
    assert calls[0]["json"] == {
        "queries": ["read recent mail", "create a GitHub issue"],
        "context_id": "ctx-old",
        "model": "hermes-4",
    }


def test_search_raises_structured_gateway_error(monkeypatch):
    _fake_http(monkeypatch, responses=[FakeResponse(403, {
        "error": {"code": "SUBSCRIPTION_REQUIRED", "message": "Subscribe first"}
    })])

    with pytest.raises(tool_provider_gateway.ToolProviderGatewayError) as exc_info:
        asyncio.run(tool_provider_gateway.search(_config(), ["read mail"]))

    assert exc_info.value.code == "SUBSCRIPTION_REQUIRED"
    assert exc_info.value.message == "Subscribe first"


def test_search_raises_transport_error(monkeypatch):
    _fake_http(monkeypatch, error=httpx.TimeoutException("request timed out"))

    with pytest.raises(tool_provider_gateway.ToolProviderTransportError, match="request timed out"):
        asyncio.run(tool_provider_gateway.search(_config(), ["read mail"]))


def test_execute_parses_results_and_sends_exact_payload(monkeypatch):
    calls = _fake_http(monkeypatch, responses=[FakeResponse(body={
        "context_id": "ctx-2",
        "results": [
            {"slug": "GMAIL_SEND_EMAIL", "successful": True, "data": {"id": "msg-1"}},
            {"slug": "GITHUB_CREATE_ISSUE", "successful": False, "error": "denied"},
        ],
    })])
    requested_tools = [
        {"slug": "GMAIL_SEND_EMAIL", "arguments": {"to": "a@example.com"}},
        {"slug": "GITHUB_CREATE_ISSUE", "arguments": {"title": "Bug"}},
    ]

    result = asyncio.run(tool_provider_gateway.execute(
        _config(), requested_tools, context_id="ctx-1", thought="Complete both tasks"
    ))

    assert result.context_id == "ctx-2"
    assert result.results[0].successful is True
    assert result.results[0].data == {"id": "msg-1"}
    assert result.results[0].error is None
    assert result.results[1].successful is False
    assert result.results[1].error == "denied"
    assert calls[0]["json"] == {
        "tools": requested_tools,
        "context_id": "ctx-1",
        "thought": "Complete both tasks",
    }


def test_schemas_parse_multiple_entries(monkeypatch):
    calls = _fake_http(monkeypatch, responses=[FakeResponse(body={"schemas": [
        {
            "slug": "GMAIL_FETCH_EMAILS",
            "toolkit": "gmail",
            "description": "Fetch emails",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "array"},
        },
        {
            "slug": "GITHUB_CREATE_ISSUE",
            "toolkit": "github",
            "description": "Create issue",
            "input_schema": {"required": ["title"]},
        },
    ]})])

    result = asyncio.run(tool_provider_gateway.schemas(
        _config(),
        ["GMAIL_FETCH_EMAILS", "GITHUB_CREATE_ISSUE"],
        context_id="ctx-1",
        include_output=True,
    ))

    assert len(result) == 2
    assert result[0].output_schema == {"type": "array"}
    assert result[1].input_schema == {"required": ["title"]}
    assert result[1].output_schema is None
    assert calls[0]["url"] == "https://tools-gateway.example.com/v1/schemas"
    assert calls[0]["json"]["include_output"] is True


def test_schemas_raise_toolkit_not_enabled_with_toolkits(monkeypatch):
    _fake_http(monkeypatch, responses=[FakeResponse(403, {"error": {
        "code": "TOOLKIT_NOT_ENABLED",
        "message": "Enable Gmail",
        "toolkits": ["gmail"],
    }})])

    with pytest.raises(tool_provider_gateway.ToolProviderGatewayError) as exc_info:
        asyncio.run(tool_provider_gateway.schemas(_config(), ["GMAIL_FETCH_EMAILS"]))

    assert exc_info.value.code == "TOOLKIT_NOT_ENABLED"
    assert exc_info.value.toolkits == ["gmail"]


@pytest.mark.parametrize(
    ("action", "response_connections"),
    [
        ("status", [
            {"toolkit": "gmail", "status": "connected", "account_hint": "a@example.com"},
            {"toolkit": "github", "status": "disconnected"},
        ]),
        ("connect", [
            {
                "toolkit": "github",
                "status": "pending",
                "connect_url": "https://connect.example/github",
            },
            {"toolkit": "gmail", "status": "connected"},
        ]),
    ],
)
def test_connections_parse_status_and_connect_responses(
    monkeypatch, action, response_connections
):
    calls = _fake_http(
        monkeypatch,
        responses=[FakeResponse(body={"connections": response_connections})],
    )

    result = asyncio.run(tool_provider_gateway.connections(
        _config(), ["gmail", "github"], action, context_id="ctx-1"
    ))

    assert [item.toolkit for item in result] == [item["toolkit"] for item in response_connections]
    assert calls[0]["json"]["action"] == action
    if action == "connect":
        assert result[0].connect_url == "https://connect.example/github"
        assert result[1].connect_url is None
    else:
        assert result[0].account_hint == "a@example.com"


def test_endpoint_urls_and_authorization_header(monkeypatch):
    calls = _fake_http(monkeypatch, responses=[
        FakeResponse(body={"context_id": "ctx", "results": [], "connections": []}),
        FakeResponse(body={"schemas": []}),
        FakeResponse(body={"context_id": "ctx", "results": []}),
        FakeResponse(body={"connections": []}),
    ])
    config = _config("https://tools-gateway.example.com/")

    asyncio.run(tool_provider_gateway.search(config, ["mail"]))
    asyncio.run(tool_provider_gateway.schemas(config, ["GMAIL_FETCH_EMAILS"]))
    asyncio.run(tool_provider_gateway.execute(config, [{"slug": "X", "arguments": {}}]))
    asyncio.run(tool_provider_gateway.connections(config, ["gmail"], "status"))

    assert [call["url"] for call in calls] == [
        "https://tools-gateway.example.com/v1/search",
        "https://tools-gateway.example.com/v1/schemas",
        "https://tools-gateway.example.com/v1/execute",
        "https://tools-gateway.example.com/v1/connections",
    ]
    assert all(call["headers"] == {
        "Authorization": "Bearer nous-token",
        "Content-Type": "application/json",
    } for call in calls)


def test_optional_request_fields_are_omitted(monkeypatch):
    calls = _fake_http(monkeypatch, responses=[
        FakeResponse(body={"context_id": "ctx", "results": [], "connections": []}),
        FakeResponse(body={"schemas": []}),
        FakeResponse(body={"context_id": "ctx", "results": []}),
        FakeResponse(body={"connections": []}),
    ])

    asyncio.run(tool_provider_gateway.search(_config(), ["mail"]))
    asyncio.run(tool_provider_gateway.schemas(_config(), ["GMAIL_FETCH_EMAILS"]))
    asyncio.run(tool_provider_gateway.execute(_config(), [{"slug": "X", "arguments": {}}]))
    asyncio.run(tool_provider_gateway.connections(_config(), ["gmail"], "status"))

    assert calls[0]["json"] == {"queries": ["mail"]}
    assert calls[1]["json"] == {"tool_slugs": ["GMAIL_FETCH_EMAILS"]}
    assert calls[2]["json"] == {"tools": [{"slug": "X", "arguments": {}}]}
    assert calls[3]["json"] == {"toolkits": ["gmail"], "action": "status"}


def test_malformed_success_response_degrades_gracefully(monkeypatch):
    _fake_http(monkeypatch, responses=[FakeResponse(body={
        "context_id": 123,
        "results": [{"tools": [{"slug": None, "input_schema": []}], "plan": "bad"}],
        "connections": [None, {"toolkit": "gmail"}],
        "guidance": "bad",
    })])

    result = asyncio.run(tool_provider_gateway.search(_config(), ["mail"]))

    assert result.context_id == "123"
    assert result.results[0].use_case == ""
    assert result.results[0].tools[0].slug == ""
    assert result.results[0].tools[0].input_schema is None
    assert result.results[0].plan == []
    assert result.connections == [{"toolkit": "gmail"}]
    assert result.guidance == []
