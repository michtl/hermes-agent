"""Target coordinates for the local hermes harness — the ONE config block.

Adding a target is a few lines here and nothing else.  Everything the harness
needs to stand a hermes profile up against a stack lives in :class:`Target`.

The two URL shapes matter and are NOT interchangeable
-----------------------------------------------------
``tools/managed_tool_gateway.build_vendor_gateway_url()`` resolves the gateway
origin for vendor ``"tools"`` from the env var ``TOOLS_GATEWAY_URL`` (the name
is derived: ``f"{vendor.upper()}_GATEWAY_URL"``).  It returns the value
verbatim apart from ``.strip().rstrip("/")``.  The request URL is then built by
``tools/tool_provider_gateway._post()`` as a naive string join::

    url = f"{config.gateway_origin.rstrip('/')}{path}"   # path == "/v1/search"

So a path-prefixed origin is legal and joins correctly.  That is load-bearing
for preview:

* ``host``  — the gateway's host-based rewrite (``{provider}-gateway.<domain>``)
  routes ``https://tools-gateway.localhost:3009/v1/search`` to the provider
  handler.  Works locally.
* ``path``  — the host rewrite NEVER matches a ``*.vercel.app`` hostname, so a
  preview deployment must address the route directly at
  ``/api/passthrough/tools/v1/search``.  Encoded as a path-prefixed origin.

Token acquisition also differs per target and is NOT a detail:

* local   — ``POST {nas}/api/internal/dev-mint-oauth-token`` (bearer
  ``dummy-auth-secret``, response field ``.accessToken``, 900s TTL).
* preview — that route is NODE_ENV-gated and returns HTTP 404
  ``{"error":"disabled_in_production"}``.  There is NO headless mint.  The
  harness must be handed a token.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

# The canonical seeded test identity.  A fresh/synthetic userId has no
# OrgMembership row, so NAS answers 403 org_access_denied and the gateway fails
# closed to an empty toolkit set — which looks exactly like a bug and is not.
DEFAULT_USER_ID = "nas_user:f7141b46-b044-41b0-aa13-a36a66f64f26"
DEFAULT_ORG_ID = "nas_organisation:cfafba9e-77f3-4f72-97e3-dc491fc90c19"
DEFAULT_CLIENT_ID = "hermes-agent"

DEFAULT_MODEL = "anthropic/claude-fable-5"


@dataclass(frozen=True)
class Target:
    """Everything needed to point a hermes profile at one stack."""

    name: str
    nas_base_url: str
    #: Value exported as ``TOOLS_GATEWAY_URL``.  May carry a path prefix.
    gateway_origin: str
    #: "host" (host-based rewrite) or "path" (direct /api/passthrough/... route).
    gateway_addressing: str
    #: "dev-mint" (headless POST to NAS) or "supplied" (human must hand one over).
    token_source: str
    #: Shown verbatim when token_source == "supplied" and no token was given.
    token_help: str = ""
    #: Bearer for the dev-mint internal route.
    mint_auth_secret: str = "dummy-auth-secret"
    user_id: str = DEFAULT_USER_ID
    org_id: str = DEFAULT_ORG_ID
    client_id: str = DEFAULT_CLIENT_ID
    #: Extra env vars stamped into the launched process.
    extra_env: Dict[str, str] = field(default_factory=dict)

    def gateway_url(self, path: str) -> str:
        """Reproduce hermes's own join so the harness proves the real URL.

        Mirrors ``tool_provider_gateway._post()`` exactly: rstrip the origin,
        concatenate the leading-slash path.  No urljoin — urljoin would eat the
        ``/api/passthrough/tools`` prefix and silently break preview.
        """
        if not path.startswith("/"):
            raise ValueError(f"gateway path must start with '/': {path!r}")
        return f"{self.gateway_origin.rstrip('/')}{path}"

    @property
    def mint_url(self) -> Optional[str]:
        if self.token_source != "dev-mint":
            return None
        return f"{self.nas_base_url.rstrip('/')}/api/internal/dev-mint-oauth-token"


TARGETS: Dict[str, Target] = {
    "local": Target(
        name="local",
        nas_base_url="http://127.0.0.1:3111",
        # Host-based rewrite: tools-gateway.localhost -> provider "tools".
        gateway_origin="http://tools-gateway.localhost:3009",
        gateway_addressing="host",
        token_source="dev-mint",
    ),
    "preview": Target(
        name="preview",
        nas_base_url="https://nas-pr-869.nousresearch.wtf",
        # PATH-DIRECT.  A *.vercel.app host can never satisfy the
        # {provider}-gateway.<domain> rewrite, so address the route itself.
        gateway_origin=(
            "https://tool-gateway-git-sid-tool-provider-v1-nousresearch.vercel.app"
            "/api/passthrough/tools"
        ),
        gateway_addressing="path",
        token_source="supplied",
        token_help=(
            "Preview has NO headless mint: POST <nas>/api/internal/dev-mint-oauth-token\n"
            "  is NODE_ENV-gated and answers HTTP 404 {\"error\":\"disabled_in_production\"}.\n"
            "  Supply a token instead:\n"
            "    --token <JWT>   or   HARNESS_TOKEN=<JWT>   or   --token-file <path>\n"
            "  Get one by completing a real browser login against the preview and\n"
            "  reading providers.nous.access_token out of that profile's auth.json,\n"
            "  or by having someone with preview NAS env access mint one server-side.\n"
            "  This is a known open loop, not a harness defect."
        ),
    ),
}


def get_target(name: str) -> Target:
    try:
        return TARGETS[name]
    except KeyError:
        known = ", ".join(sorted(TARGETS))
        raise SystemExit(f"unknown target {name!r}; known targets: {known}") from None
