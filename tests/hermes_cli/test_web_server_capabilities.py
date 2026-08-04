"""Tests for the dashboard's Nous Portal capabilities proxy."""

import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.web_routers import capabilities as capabilities_module

_PORTAL_URL = "https://portal.example.test"
_TEST_TOKEN = "test-bearer-token-must-not-leak"


@pytest.fixture
def client():
    """Create a dashboard client with the outer dashboard auth gate disabled."""
    previous_auth_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    try:
        yield test_client
    finally:
        if previous_auth_required is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous_auth_required


@pytest.fixture
def fixed_portal_auth(monkeypatch):
    """Resolve every route request to a stable fake Portal credential."""
    async def resolve_portal_auth():
        return _PORTAL_URL, _TEST_TOKEN

    monkeypatch.setattr(
        capabilities_module,
        "_resolve_portal_auth",
        resolve_portal_auth,
    )


def _mock_async_client(monkeypatch, handler):
    """Make the production request helper use an httpx mock transport."""
    transport = httpx.MockTransport(handler)
    original_async_client = httpx.AsyncClient

    def async_client(*, timeout):
        assert timeout == capabilities_module._TIMEOUT_SECONDS
        return original_async_client(transport=transport, timeout=timeout)

    monkeypatch.setattr(capabilities_module.httpx, "AsyncClient", async_client)


def test_list_toolkits_translates_all_override_states(
    client,
    fixed_portal_auth,
    monkeypatch,
):
    nas_response = {
        "toolkits": [
            {
                "slug": "github",
                "name": "GitHub",
                "enabled": True,
                "connected": True,
                "logo": "https://cdn.example.test/github.svg",
            },
            {
                "slug": "slack",
                "name": "Slack",
                "enabled": True,
                "connected": True,
                "logo": "https://cdn.example.test/slack.svg",
                "tools": [],
            },
            {
                "slug": "notion",
                "name": "Notion",
                "enabled": True,
                "connected": True,
                "logo": "https://cdn.example.test/notion.svg",
                "tools": ["NOTION_SEARCH", "NOTION_FETCH"],
            },
        ]
    }

    def handler(request):
        assert request.method == "GET"
        assert request.url == f"{_PORTAL_URL}/api/portal/tools/toolkits"
        assert request.headers["authorization"] == f"Bearer {_TEST_TOKEN}"
        return httpx.Response(200, json=nas_response)

    _mock_async_client(monkeypatch, handler)

    response = client.get("/api/capabilities/toolkits")

    assert response.status_code == 200
    toolkits = response.json()["toolkits"]
    assert toolkits[0]["toolsOverride"] is None
    assert toolkits[1]["toolsOverride"] == []
    assert toolkits[2]["toolsOverride"] == ["NOTION_SEARCH", "NOTION_FETCH"]
    assert toolkits[0]["toolsOverride"] != toolkits[1]["toolsOverride"]
    assert all("tools" not in toolkit for toolkit in toolkits)


def test_list_toolkits_returns_401_when_not_logged_in(client, monkeypatch):
    async def not_logged_in():
        raise HTTPException(status_code=401, detail="Not logged into Nous Portal")

    monkeypatch.setattr(
        capabilities_module,
        "_resolve_portal_auth",
        not_logged_in,
    )

    response = client.get("/api/capabilities/toolkits")

    assert response.status_code == 401
    assert response.json() == {"detail": "Not logged into Nous Portal"}
    assert _TEST_TOKEN not in json.dumps(response.json())


def test_set_toolkit_enabled_forwards_body(
    client,
    fixed_portal_auth,
    monkeypatch,
):
    expected = {
        "slug": "github",
        "enabled": True,
        "enabledToolkits": ["github"],
        "toolsOverride": None,
    }

    def handler(request):
        assert request.method == "PUT"
        assert request.url == f"{_PORTAL_URL}/api/portal/tools/toolkits/github"
        assert json.loads(request.content) == {"enabled": True}
        return httpx.Response(
            200,
            json={
                "slug": "github",
                "enabled": True,
                "enabledToolkits": ["github"],
                "toolOverrides": {},
            },
        )

    _mock_async_client(monkeypatch, handler)

    response = client.put(
        "/api/capabilities/toolkits/github",
        json={"enabled": True},
    )

    assert response.status_code == 200
    assert response.json() == expected


@pytest.mark.parametrize(
    ("request_body", "expected_body", "nas_overrides", "expected_override"),
    [
        ({"enabled": True}, {"enabled": True}, {}, None),
        (
            {"enabled": True, "tools": []},
            {"enabled": True, "tools": []},
            {"github": []},
            [],
        ),
        (
            {"enabled": True, "tools": ["GITHUB_GET_REPOS", "GITHUB_GET_ISSUES"]},
            {
                "enabled": True,
                "tools": ["GITHUB_GET_REPOS", "GITHUB_GET_ISSUES"],
            },
            {"github": ["GITHUB_GET_REPOS", "GITHUB_GET_ISSUES"]},
            ["GITHUB_GET_REPOS", "GITHUB_GET_ISSUES"],
        ),
    ],
    ids=("tools-omitted", "deny-all", "populated-scope"),
)
def test_set_toolkit_scope_preserves_tools_field_semantics(
    client,
    fixed_portal_auth,
    monkeypatch,
    request_body,
    expected_body,
    nas_overrides,
    expected_override,
):
    def handler(request):
        assert request.method == "PUT"
        assert request.url == f"{_PORTAL_URL}/api/portal/tools/toolkits/github"
        assert json.loads(request.content) == expected_body
        return httpx.Response(
            200,
            json={
                "slug": "github",
                "enabled": True,
                "enabledToolkits": ["github"],
                "toolOverrides": nas_overrides,
            },
        )

    _mock_async_client(monkeypatch, handler)

    response = client.put(
        "/api/capabilities/toolkits/github",
        json=request_body,
    )

    assert response.status_code == 200
    assert response.json() == {
        "slug": "github",
        "enabled": True,
        "enabledToolkits": ["github"],
        "toolsOverride": expected_override,
    }
    if expected_override == []:
        assert response.json()["toolsOverride"] is not None


def test_set_toolkit_scope_forwards_explicit_null_as_clear_signal(
    client,
    fixed_portal_auth,
    monkeypatch,
):
    """An explicit `tools: null` (the dashboard's "Clear override" action)
    must be forwarded to NAS as an explicit null, distinct from an omitted
    field, which now means "preserve the existing scope"."""

    def handler(request):
        assert request.method == "PUT"
        assert request.url == f"{_PORTAL_URL}/api/portal/tools/toolkits/github"
        body = json.loads(request.content)
        assert "tools" in body
        assert body["tools"] is None
        return httpx.Response(
            200,
            json={
                "slug": "github",
                "enabled": True,
                "enabledToolkits": ["github"],
                "toolOverrides": {},
            },
        )

    _mock_async_client(monkeypatch, handler)

    response = client.put(
        "/api/capabilities/toolkits/github",
        json={"enabled": True, "tools": None},
    )

    assert response.status_code == 200
    assert response.json()["toolsOverride"] is None


def test_connect_unknown_toolkit_preserves_400_detail(
    client,
    fixed_portal_auth,
    monkeypatch,
):
    def handler(request):
        assert request.method == "POST"
        assert request.url == (
            f"{_PORTAL_URL}/api/portal/tools/toolkits/unknown/connect"
        )
        assert request.content == b""
        return httpx.Response(400, json={"error": "unknown_toolkit"})

    _mock_async_client(monkeypatch, handler)

    response = client.post(
        "/api/capabilities/toolkits/unknown/connect",
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "unknown_toolkit"}


def test_nas_timeout_returns_502_without_token(
    client,
    fixed_portal_auth,
    monkeypatch,
):
    def handler(request):
        raise httpx.ReadTimeout("NAS did not respond", request=request)

    _mock_async_client(monkeypatch, handler)

    response = client.get("/api/capabilities/toolkits")

    assert response.status_code == 502
    assert response.json() == {"detail": "Timed out contacting Nous Portal"}
    assert _TEST_TOKEN not in json.dumps(response.json())
