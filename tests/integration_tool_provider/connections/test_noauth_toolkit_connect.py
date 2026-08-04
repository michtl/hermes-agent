"""Lens: adversarial re-derivation of DELEG-5 — app_connections action='connect'
on a NO-AUTH toolkit (hackernews).

DELEG-5 claimed the failure through a delegated live CLI run, i.e. mixed a
gateway-side fact with a model-behaviour anecdote. This file separates them and
proves ONLY the deterministic, model-free half:

  1. LIVE (opt-in, DELEG5_LIVE=1): POST /v1/connections action='status' on
     hackernews reports status='disconnected' even though hackernews needs no
     auth — this is what invites a 'connect' attempt in the first place.
  2. LIVE (opt-in): POST /v1/connections action='connect' on hackernews returns
     HTTP 502 {"error":{"code":"UPSTREAM_ERROR", ...}} deterministically,
     N-for-N, with NO model in the loop. The auth-requiring control (github)
     returns HTTP 200 with a connect_url on the same token, so the failure is
     specific to the no-auth toolkit and is not a token/entitlement problem.
  3. OFFLINE (always runs): given that gateway response, the bridge surfaces a
     non-actionable, non-recoverable tool_error to the model. The contract
     (docs/tool-provider-v1-contract.md, POST /v1/connections) specifies
     connect_url as "present when action='connect' and auth is needed",
     i.e. a 200 with no connect_url is the sanctioned no-auth-needed shape —
     and app_connections_tool ALREADY has the branch for it
     ("No new authorization was needed ..."). That branch is unreachable for a
     no-auth toolkit because the gateway 502s before it.

Root cause (read from the READ-ONLY gateway reference, not guessed):
  src/server/providers/composio/provider.ts connections() action='connect'
  unconditionally calls getOrCreateManagedAuthConfigId(toolkit), which does
  authConfigs.list({toolkit, isComposioManaged:true}) -> 0 items for a no-auth
  toolkit -> authConfigs.create(toolkit, {type:"use_composio_managed_auth"}),
  and upstream rejects that with HTTP 400 Auth_Config_NoAuthApp
  ("...because it does not require authentication"). The route handler maps the
  unhandled provider error to a 502 UPSTREAM_ERROR. There is no no-auth branch
  anywhere in the gateway connect path.

NOT re-derived here (explicitly out of scope, and NOT claimed): that a model
loops on the error. One transcript is not evidence of a code defect.

Run (offline only, the default):
    cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge && \
    TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 .venv/bin/python -m pytest \
    tests/integration_tool_provider/connections/test_noauth_toolkit_connect.py -q

Run including the live gateway probes:
    cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge && \
    DELEG5_LIVE=1 TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 .venv/bin/python -m pytest \
    tests/integration_tool_provider/connections/test_noauth_toolkit_connect.py -q -s
"""

import json
import os
import urllib.error
import urllib.request
from unittest.mock import AsyncMock, Mock

import pytest

from tools import app_connections_tool
from tools.tool_provider_gateway import ToolProviderGatewayError

_VENDOR_LITERAL = "composio"

NAS_URL = "http://127.0.0.1:3111"
GATEWAY_URL = "http://tools-gateway.localhost:3009"
USER_ID = "nas_user:f7141b46-b044-41b0-aa13-a36a66f64f26"
ORG_ID = "nas_organisation:cfafba9e-77f3-4f72-97e3-dc491fc90c19"

_LIVE = os.environ.get("DELEG5_LIVE") == "1"
live_only = pytest.mark.skipif(not _LIVE, reason="set DELEG5_LIVE=1 to hit the live gateway")


def _post(url: str, body: dict, headers: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


@pytest.fixture(scope="module")
def token() -> str:
    if not _LIVE:
        pytest.skip("live-only")
    status, payload = _post(
        f"{NAS_URL}/api/internal/dev-mint-oauth-token",
        {"userId": USER_ID, "orgId": ORG_ID, "clientId": "hermes-agent"},
        {"Authorization": "Bearer dummy-auth-secret"},
    )
    assert status == 200, (status, payload)
    return payload["accessToken"]


# ---------------------------------------------------------------- live probes


@live_only
def test_status_reports_noauth_toolkit_as_disconnected(token):
    """The bait: a toolkit that needs no auth still reads 'disconnected'."""
    status, payload = _post(
        f"{GATEWAY_URL}/v1/connections",
        {"toolkits": ["hackernews"], "action": "status"},
        {"Authorization": f"Bearer {token}"},
    )
    print(f"\n[status hackernews] HTTP {status} {json.dumps(payload)}")
    assert status == 200
    assert payload["connections"][0]["status"] == "disconnected"
    # No connect_url and no hint that connecting is unnecessary -> nothing tells
    # a caller that 'disconnected' is terminal-but-fine for this toolkit.
    assert "connect_url" not in payload["connections"][0]


@live_only
def test_connect_on_noauth_toolkit_502s_deterministically(token):
    """3-for-3, no model involved. Determinism is the whole point."""
    seen = []
    for _ in range(3):
        status, payload = _post(
            f"{GATEWAY_URL}/v1/connections",
            {"toolkits": ["hackernews"], "action": "connect"},
            {"Authorization": f"Bearer {token}"},
        )
        seen.append((status, payload.get("error", {}).get("code")))
    print(f"\n[connect hackernews x3] {seen}")
    assert seen == [(502, "UPSTREAM_ERROR")] * 3, seen


@live_only
def test_connect_on_auth_requiring_toolkit_succeeds_same_token(token):
    """Control: the same token/user/org connects github fine -> not auth/entitlement."""
    status, payload = _post(
        f"{GATEWAY_URL}/v1/connections",
        {"toolkits": ["github"], "action": "connect"},
        {"Authorization": f"Bearer {token}"},
    )
    conn = payload["connections"][0]
    print(f"\n[connect github] HTTP {status} status={conn['status']} has_connect_url={'connect_url' in conn}")
    assert status == 200
    assert "connect_url" in conn


# ------------------------------------------------------------- offline probes


def _dispatch_with_gateway_error() -> str:
    """Bridge behaviour given the gateway 502 proven above."""
    app_connections_tool._resolve_gateway = Mock(return_value={"base_url": "x", "token": "y"})
    err = ToolProviderGatewayError("UPSTREAM_ERROR", "Upstream provider request failed")
    import tools.tool_provider_gateway as gw

    orig = gw.connections
    gw.connections = AsyncMock(side_effect=err)
    try:
        return app_connections_tool._dispatch_app_connections(
            {"action": "connect", "toolkits": ["hackernews"]}
        )
    finally:
        gw.connections = orig


def test_bridge_surfaces_gateway_502_verbatim_and_non_actionably():
    """The exact string DELEG-5 quoted, re-derived from the bridge, not a transcript."""
    out = json.loads(_dispatch_with_gateway_error())
    print(f"\n[app_connections connect hackernews] {json.dumps(out)}")
    assert out == {"error": "Upstream provider request failed", "code": "UPSTREAM_ERROR"}
    # Non-actionable: nothing tells the caller that hackernews needs no auth,
    # nothing distinguishes this from a transient fault, so an identical retry
    # is the locally-rational next move.
    assert "auth" not in json.dumps(out).lower()
    assert _VENDOR_LITERAL not in json.dumps(out).lower()


def test_bridge_has_an_unreachable_no_auth_needed_branch():
    """app_connections ALREADY handles 'connect but nothing to authorize' —
    a 200 with no connect_url. The gateway just never produces that shape for a
    no-auth toolkit, so this branch is dead for exactly the toolkits it fits."""
    app_connections_tool._resolve_gateway = Mock(return_value={"base_url": "x", "token": "y"})
    import tools.tool_provider_gateway as gw

    orig = gw.connections
    conn = Mock(toolkit="hackernews", status="connected", connect_url=None, account_hint=None)
    gw.connections = AsyncMock(return_value=[conn])
    try:
        out = json.loads(
            app_connections_tool._dispatch_app_connections(
                {"action": "connect", "toolkits": ["hackernews"]}
            )
        )
    finally:
        gw.connections = orig
    print(f"\n[hypothetical contract-shaped 200] {json.dumps(out)}")
    assert "No new authorization was needed" in out["note"]
    assert "error" not in out
