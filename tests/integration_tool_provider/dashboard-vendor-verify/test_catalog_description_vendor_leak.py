"""Independent re-derivation of the "vendor name rendered in catalog card" finding.

Does NOT reuse the original reporter's Playwright harness. Proves the leak as a
data-plus-code chain:

  1. LIVE: NAS's catalog serves a toolkit whose free-text `description` contains
     the vendor's name.
  2. CODE: the hermes proxy passes `description` through untouched.
  3. CODE: the client merge prefers the SERVER description over any local blurb.
  4. CODE: CatalogGrid renders `{toolkit.description}` verbatim, unconditionally,
     with no scrub/redact/filter of vendor-name substrings.

Run:
  cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge && \
    TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 .venv/bin/python -m pytest \
    tests/integration_tool_provider/dashboard-vendor-verify/test_catalog_description_vendor_leak.py -q

Never prints the minted JWT.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
NAS = "http://127.0.0.1:3111"
USER_ID = "nas_user:f7141b46-b044-41b0-aa13-a36a66f64f26"
ORG_ID = "nas_organisation:cfafba9e-77f3-4f72-97e3-dc491fc90c19"

# Scan pattern only. Never emitted to model- or user-visible output.
VENDOR = "composio"


def _curl(args: list[str]) -> str:
    return subprocess.run(
        ["curl", "-s", *args], capture_output=True, text=True, timeout=30
    ).stdout


@pytest.fixture(scope="module")
def catalog() -> dict:
    token = json.loads(
        _curl(
            [
                "-X", "POST", f"{NAS}/api/internal/dev-mint-oauth-token",
                "-H", "Authorization: Bearer dummy-auth-secret",
                "-H", "Content-Type: application/json",
                "-d", json.dumps({"userId": USER_ID, "orgId": ORG_ID,
                                  "clientId": "hermes-agent"}),
            ]
        )
    )["accessToken"]
    body = _curl([f"{NAS}/api/portal/tools/toolkits",
                  "-H", f"Authorization: Bearer {token}"])
    return json.loads(body)


def test_live_nas_description_field_contains_vendor_name(catalog: dict) -> None:
    """1. The leak exists in live data, in the `description` field."""
    toolkits = catalog["toolkits"]
    assert len(toolkits) == 100, f"unexpected catalog size {len(toolkits)}"

    leaking = [
        t for t in toolkits
        if VENDOR in (t.get("description") or "").lower()
    ]
    slugs = sorted(t["slug"] for t in leaking)
    assert slugs == ["browser_tool"], f"description leaks changed: {slugs}"

    desc = leaking[0]["description"]
    # The vendor name is the FIRST word of user-facing prose.
    assert desc.lower().startswith(VENDOR), desc[:40]


def test_logo_field_leaks_vendor_domain_but_is_never_rendered(catalog: dict) -> None:
    """Scope check: `logo` leaks the vendor domain on all 100 entries, but the
    dashboard never renders it (BrandGlyph draws initials), so it does not reach
    the DOM. Transported over the wire only."""
    toolkits = catalog["toolkits"]
    logo_leaks = [t for t in toolkits if VENDOR in (t.get("logo") or "").lower()]
    assert len(logo_leaks) == 100

    src = (REPO / "web/src/pages/CapabilitiesPage.tsx").read_text()
    assert "toolkit.logo" not in src
    assert "toolkitInitials(toolkit.name)" in src


def test_proxy_passes_description_through_untouched() -> None:
    """2. The hermes backend proxy only renames `tools`; no scrub."""
    src = (REPO / "hermes_cli/web_routers/capabilities.py").read_text()
    handler = src.split('@router.get("/api/capabilities/toolkits")')[1].split(
        "@router.put"
    )[0]
    assert 'normalized["toolsOverride"] = normalized.pop("tools", None)' in handler
    assert "description" not in handler
    assert not re.search(r"scrub|redact|sanitiz", handler, re.I)


def test_merge_prefers_server_description() -> None:
    """3. Server description wins over the local curated blurb."""
    src = (REPO / "web/src/lib/capability-catalog.ts").read_text()
    assert (
        "description: toolkit.description ?? catalogEntry?.description ?? \"\""
        in src
    )
    # browser_tool has no local curated entry anyway.
    assert "browser_tool" not in src


def test_catalog_grid_renders_description_verbatim_with_no_scrub() -> None:
    """4. Render site: unconditional verbatim interpolation, no slug exclusion."""
    src = (REPO / "web/src/pages/CapabilitiesPage.tsx").read_text()

    grid = src.split("function CatalogGrid(")[1].split("interface StarterBannerProps")[0]
    assert "{toolkit.description}" in grid
    # No scrubbing anywhere on the page, and no slug-level exclusion list.
    assert not re.search(r"scrub|redact|sanitiz", src, re.I)
    assert "browser_tool" not in src

    # The only filter is category + name search; description is never filtered
    # and the default view (category "All", empty query) shows every toolkit.
    filt = src.split("const filteredToolkits = useMemo(")[1].split("}, [")[0]
    assert 'category === "All"' in filt
    assert "toolkit.name.toLocaleLowerCase().includes(query)" in filt
    assert "description" not in filt
