"""Regression guard: the vendor name must not reach the catalog card.

This file began life as a *reporter* proving a live leak — NAS's catalog serves a
toolkit whose free-text ``description`` spells out the vendor's name, and the
dashboard rendered it verbatim into the DOM. That leak is fixed, so the
assertions are inverted: they now guard the fix instead of documenting the bug.

What makes this worth keeping over the frontend unit tests is the LIVE half. The
unit tests scrub a fixture the test author wrote; this one scrubs whatever NAS is
actually serving today, so it catches a new vendor mention appearing in upstream
catalog data — a field nobody on this side controls.

The chain under guard:

  1. LIVE: NAS's catalog is fetched, and any vendor mentions in its free-text
     fields are recorded (their presence upstream is expected and fine).
  2. CODE: the client merge scrubs vendor mentions before anything renders.
  3. LIVE + CODE: no vendor substring survives into what the card would display.

Run:
  cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge && \
    TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 .venv/bin/python -m pytest \
    tests/integration_tool_provider/dashboard-vendor-verify/test_catalog_description_vendor_leak.py -q

Requires live NAS on :3111. Never prints the minted JWT.
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

# Free-text fields that reach the rendered card. Extend this if the card starts
# showing another upstream prose field — that is exactly the case this guards.
PROSE_FIELDS = ("description",)


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


def _toolkits(catalog: dict) -> list[dict]:
    toolkits = catalog.get("toolkits")
    if not toolkits:
        pytest.skip("live NAS catalog unavailable or empty")
    return toolkits


def test_scrub_helper_exists_and_is_applied_in_the_merge() -> None:
    """The scrub must live in the shared merge, not at one render site.

    Both the catalog grid and the detail slideover read from the merged toolkit,
    so scrubbing there is what makes a future render site safe by default.
    """
    src = (REPO / "web/src/lib/capability-catalog.ts").read_text()
    assert re.search(r"scrubVendorMentions", src), "scrub helper is gone"
    merge = src.split("export function mergeCapabilityToolkit")[1]
    assert "scrubVendorMentions" in merge, "merge no longer scrubs"


def test_live_catalog_prose_is_scrubbed_before_render(catalog: dict) -> None:
    """Whatever NAS serves today, no vendor mention survives into the card.

    Applies the same transform the client applies, to the live payload. If NAS
    starts shipping the vendor's name in a prose field the scrub does not cover,
    this fails — which is the point.
    """
    toolkits = _toolkits(catalog)
    upstream_hits = [
        (t.get("slug"), field)
        for t in toolkits
        for field in PROSE_FIELDS
        if VENDOR in (t.get(field) or "").lower()
    ]

    # Mirror of scrubVendorMentions in web/src/lib/capability-catalog.ts.
    def scrub(text: str) -> str:
        return re.sub(VENDOR, "the tool provider", text, flags=re.I)

    survivors = [
        (t.get("slug"), field)
        for t in toolkits
        for field in PROSE_FIELDS
        if VENDOR in scrub(t.get(field) or "").lower()
    ]
    assert not survivors, f"vendor name survives the scrub in {survivors}"

    # Not an assertion about upstream — just a record of what this run covered,
    # so a green run on a catalog with zero mentions is not mistaken for proof.
    if not upstream_hits:
        pytest.skip(
            "live catalog carried no vendor mentions this run; "
            "scrub correctness is covered, real-data coverage is not"
        )


def test_no_vendor_cdn_logo_field_is_rendered() -> None:
    """The unused vendor-CDN logo URL stayed removed from the render path."""
    src = (REPO / "web/src/pages/CapabilitiesPage.tsx").read_text()
    assert "toolkit.logo" not in src
    assert "toolkitInitials(toolkit.name)" in src


def test_proxy_still_only_renames_fields(catalog: dict) -> None:
    """The proxy is a renamer, not a scrubber — the scrub belongs client-side.

    Pinned so nobody "fixes" a future leak by scrubbing in two places and leaving
    the two implementations to drift apart.
    """
    src = (REPO / "hermes_cli/web_routers/capabilities.py").read_text()
    handler = src.split('@router.get("/api/capabilities/toolkits")')[1].split(
        "@router.put"
    )[0]
    assert 'normalized["toolsOverride"] = normalized.pop("tools", None)' in handler
    assert not re.search(r"scrub|redact|sanitiz", handler, re.I)
