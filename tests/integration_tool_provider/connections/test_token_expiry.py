"""Lens: TOKEN EXPIRY, done the cheap way — one real wire call, no waiting.

Crafts an already-expired HS256 JWT (signed with the dev-mint shared secret
"dummy-auth-secret", claim shape copied from a real minted token but with
`exp`/`iat` moved into the past) and exercises the bridge client against the
LIVE gateway with it. No 900s wait, no real token aging — the token is
synthetically expired at construction time.

Marked `integration` (real network, live dependency) so it is excluded from
default `-m 'not integration'` runs, same convention as the rest of the repo.
Run explicitly:

    cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge && \
    TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 .venv/bin/python -m pytest \
    tests/integration_tool_provider/connections/test_token_expiry.py -q -m integration

Findings captured here (see module docstring end for the plain-English
verdict — also echoed by the orchestrator's summary):

  - The live gateway answers an expired token with HTTP 401 and a clean,
    structured body: {"error": {"code": "AUTH_ERROR", "message":
    "Unauthorized"}, "requestId": "..."}. No vendor name, no raw upstream
    text, no stack trace.
  - tools/tool_provider_gateway.py's `_post()` turns that into a typed
    ToolProviderGatewayError(code="AUTH_ERROR", message="Unauthorized") —
    the same typed-error path a 403/other refusal takes elsewhere in this
    codebase's test suite.
  - app_connections_tool._dispatch_app_connections surfaces this to the
    model as tool_error("Unauthorized", code="AUTH_ERROR") — clean JSON,
    no crash, no vendor name.
  - agent_init.py's session-init probe swallows this exact failure the same
    way it swallows any other connections-probe exception (see
    test_session_init_probe.py's mocked equivalent): session start still
    succeeds, _tool_provider_connected_toolkits falls back to None.
  - NO retry-on-401 and NO reactive token refresh exist at this layer: the
    gateway client (tools/tool_provider_gateway.py) makes exactly one POST
    with whatever token it was handed and raises on 401; it never inspects
    or refreshes the token itself. (Proactive refresh only happens one layer
    up, in tools/managed_tool_gateway.py's read_nous_access_token(), which
    refreshes based on the LOCAL auth-store's cached `expires_at` bookkeeping
    *before* a request — never reactively off a 401 response. A token that
    is expired-but-not-yet-known-expired-locally, or an out-of-band token
    like the one crafted here, gets NO automatic recovery: the caller must
    re-authenticate and a running process must be relaunched with a fresh
    token, consistent with KNOWN EDGE #4.)
  - Verdict: demo-safe. An expired/invalid token degrades to a clear typed
    error at every layer that surfaces to the model, never a crash, never a
    vendor name, never a raw traceback. The one operator action required is
    the already-documented one — re-mint and relaunch the process holding
    the token; nothing here makes that worse.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from tools.tool_provider_gateway import (
    ToolProviderGatewayError,
    ToolProviderTransportError,
    connections as gw_connections,
)

pytestmark = pytest.mark.integration

_GATEWAY_URL = "http://tools-gateway.localhost:3009"
_DEV_MINT_SECRET = "dummy-auth-secret"  # shared dev-only secret, not a real credential
_VENDOR_LITERAL = "composio"
_TEST_USER = "nas_user:f7141b46-b044-41b0-aa13-a36a66f64f26"
_TEST_ORG = "nas_organisation:cfafba9e-77f3-4f72-97e3-dc491fc90c19"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_expired_hs256_jwt() -> str:
    """Stdlib-only HS256 JWT encoder — no PyJWT dependency needed.

    Claim shape copied from a real dev-minted token's decoded payload
    (structure only; this test never touches or prints a real minted token),
    with `iat`/`exp` both moved into the past so the token is expired at
    construction, not by waiting.
    """
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "client_id": "hermes-agent",
        "org_id": _TEST_ORG,
        "scope": "inference:invoke tool:invoke",
        "token_use": "access",
        "oauth_contract_version": 1,
        "sid": "dev-mint:expired-probe-0001",
        "product_id": "nous-hermes-agent",
        "nous_client": "hermes-agent",
        "tool_gateway_admin": False,
        "paid_access": True,
        "privacy_mode": "standard",
        "policy_present": False,
        "subscription_tier": 1,
        "sub": _TEST_USER,
        "iss": "http://127.0.0.1:3111",
        "aud": "hermes-cli:hermes-agent",
        "iat": now - 3000,
        "exp": now - 1000,  # expired 1000s ago
        "jti": "expired-probe-jti-0001",
    }
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()
    sig = hmac.new(_DEV_MINT_SECRET.encode(), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url(sig)}"


@pytest.fixture(scope="module")
def expired_token() -> str:
    return _make_expired_hs256_jwt()


@pytest.fixture(scope="module")
def live_gateway_config(expired_token):
    return SimpleNamespace(
        vendor="tools",
        gateway_origin=_GATEWAY_URL,
        nous_user_token=expired_token,
        managed_mode=True,
    )


def _skip_if_unreachable(exc: Exception):
    if isinstance(exc, ToolProviderTransportError):
        pytest.skip(f"tools-gateway not reachable in this environment: {exc}")


# ---------------------------------------------------------------------------
# Wire-level: what the gateway itself does with an expired token
# ---------------------------------------------------------------------------


def test_expired_token_gateway_client_raises_typed_auth_error(live_gateway_config):
    """One real call: gw.connections() against the live gateway with an
    expired token must raise a typed, clean ToolProviderGatewayError — never
    hang, never leak a vendor name or raw upstream text.
    """
    try:
        with pytest.raises(ToolProviderGatewayError) as excinfo:
            asyncio.run(gw_connections(live_gateway_config, [], "status", context_id=None))
    except ToolProviderTransportError as exc:  # pragma: no cover - env guard
        _skip_if_unreachable(exc)
        raise

    err = excinfo.value
    assert err.code  # some structured code came back
    assert _VENDOR_LITERAL not in err.message.lower()
    assert "traceback" not in err.message.lower()
    # This is a real live assertion, not just "any 4xx": the gateway's
    # auth-refusal shape as observed live at capture time.
    assert err.code == "AUTH_ERROR"
    assert err.message == "Unauthorized"


def test_expired_token_no_retry_or_silent_refresh_at_client_layer(live_gateway_config):
    """Two consecutive real calls with the SAME expired token must fail
    identically both times — proving the gateway client neither retries nor
    silently swaps in a refreshed token on its own. (Proactive refresh, where
    it exists, lives one layer up in managed_tool_gateway.py and is keyed off
    local auth-store bookkeeping, not off a 401 response — see module
    docstring.)
    """
    async def _call_twice():
        results = []
        for _ in range(2):
            try:
                await gw_connections(live_gateway_config, [], "status", context_id=None)
                results.append(None)
            except ToolProviderGatewayError as exc:
                results.append((exc.code, exc.message))
        return results

    try:
        results = asyncio.run(_call_twice())
    except ToolProviderTransportError as exc:  # pragma: no cover - env guard
        _skip_if_unreachable(exc)
        raise

    assert results[0] == results[1] == ("AUTH_ERROR", "Unauthorized")


# ---------------------------------------------------------------------------
# What the model sees: app_connections tool output with an expired token
# ---------------------------------------------------------------------------


def test_expired_token_model_visible_output_is_clean_json(monkeypatch, live_gateway_config):
    from tools import app_connections_tool

    monkeypatch.setattr(app_connections_tool, "_resolve_gateway", lambda: live_gateway_config)

    raw = app_connections_tool._dispatch_app_connections(
        {"action": "status", "toolkits": ["gmail"]}
    )
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        pytest.fail(f"app_connections did not return clean JSON on expired-token failure: {raw!r}")

    assert "error" in result
    assert _VENDOR_LITERAL not in raw.lower()
    assert "traceback" not in raw.lower()
    assert result == {"error": "Unauthorized", "code": "AUTH_ERROR"}


# ---------------------------------------------------------------------------
# Session init: does an expired token at init time still let the session start?
# ---------------------------------------------------------------------------


def test_expired_token_session_init_still_succeeds(monkeypatch, capsys, live_gateway_config):
    """Real (not mocked) network call inside init_agent's connections probe,
    using the expired token. Session start must still complete.
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
        patch("tools.tool_backend_helpers.managed_nous_tools_enabled", return_value=True),
        patch(
            "tools.tool_provider_gateway.resolve_tool_provider_gateway",
            return_value=live_gateway_config,
        ),
        # NOTE: tools.tool_provider_gateway.connections is deliberately NOT
        # mocked here — this makes a real network call to the live gateway
        # with the expired token, exercising the actual code path end to end.
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

    assert agent._tool_provider_context_id is None
    assert agent._tool_provider_connected_toolkits is None

    out, err = capsys.readouterr()
    combined = (out + err).lower()
    assert _VENDOR_LITERAL not in combined
    assert "traceback" not in combined
