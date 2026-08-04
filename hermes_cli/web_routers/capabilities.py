"""Nous Portal capability routes for the dashboard."""

import asyncio
import logging
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException

from hermes_cli.web_models import ToolkitEnabledUpdate

_log = logging.getLogger("hermes_cli.web_server")

router = APIRouter()

_TIMEOUT_SECONDS = 10.0
_PASSTHROUGH_ERROR_STATUSES = frozenset({400, 401, 403, 404})


async def _resolve_portal_auth() -> tuple[str, str]:
    """Resolve the portal URL and bearer token for an authenticated NAS call.

    The local auth snapshot supplies the configured Portal URL. Token
    resolution runs in a worker thread because refreshing it may perform a
    blocking network request. Missing authentication is exposed as HTTP 401.
    """
    from hermes_cli.auth import (
        AuthError,
        DEFAULT_NOUS_PORTAL_URL,
        get_nous_auth_status_local,
        resolve_nous_access_token,
    )

    status = get_nous_auth_status_local() or {}
    portal_url = (
        str(status.get("portal_base_url") or "").rstrip("/")
        or DEFAULT_NOUS_PORTAL_URL.rstrip("/")
    )
    try:
        token = await asyncio.to_thread(resolve_nous_access_token)
    except AuthError as exc:
        raise HTTPException(
            status_code=401,
            detail="Not logged into Nous Portal",
        ) from exc
    return portal_url, token


async def _nas_request(
    method: str,
    portal_url: str,
    path: str,
    token: str,
    json_body: Optional[Dict[str, Any]] = None,
) -> Any:
    """Proxy one NAS portal API call and return its parsed JSON response.

    Requests time out after ten seconds. NAS 400, 401, 403, and 404 responses
    preserve their status and expose an upstream ``error`` or ``message`` as
    the detail. Other upstream errors, transport failures, and invalid JSON
    become HTTP 502. The bearer credential is never logged or returned.
    """
    url = f"{portal_url}{path}"
    request_kwargs: Dict[str, Any] = {
        "headers": {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
    }
    if json_body is not None:
        request_kwargs["json"] = json_body
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.request(
                method,
                url,
                **request_kwargs,
            )
    except httpx.TimeoutException as exc:
        _log.warning("capabilities: NAS %s %s timed out", method, path)
        raise HTTPException(
            status_code=502,
            detail="Timed out contacting Nous Portal",
        ) from exc
    except httpx.HTTPError as exc:
        _log.warning(
            "capabilities: NAS %s %s failed: %s",
            method,
            path,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail="Failed to contact Nous Portal",
        ) from exc

    if response.status_code >= 400:
        _log.warning(
            "capabilities: NAS %s %s returned %s",
            method,
            path,
            response.status_code,
        )
        if response.status_code not in _PASSTHROUGH_ERROR_STATUSES:
            raise HTTPException(
                status_code=502,
                detail="Nous Portal request failed",
            )

        detail = f"Nous Portal returned {response.status_code}"
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = str(body.get("error") or body.get("message") or detail)
        except ValueError:
            pass
        raise HTTPException(status_code=response.status_code, detail=detail)

    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="Nous Portal returned an invalid response",
        ) from exc


@router.get("/api/capabilities/toolkits")
async def list_toolkits():
    """Proxy NAS's toolkit catalog and per-organization state."""
    portal_url, token = await _resolve_portal_auth()
    result = await _nas_request(
        "GET",
        portal_url,
        "/api/portal/tools/toolkits",
        token,
    )
    if isinstance(result, dict) and isinstance(result.get("toolkits"), list):
        normalized_toolkits = []
        for toolkit in result["toolkits"]:
            if not isinstance(toolkit, dict):
                normalized_toolkits.append(toolkit)
                continue
            normalized = dict(toolkit)
            normalized["toolsOverride"] = normalized.pop("tools", None)
            normalized_toolkits.append(normalized)
        result = {**result, "toolkits": normalized_toolkits}
    return result


@router.put("/api/capabilities/toolkits/{slug}")
async def set_toolkit_enabled(slug: str, body: ToolkitEnabledUpdate):
    """Proxy NAS's enable or disable toggle for one toolkit."""
    portal_url, token = await _resolve_portal_auth()
    json_body: Dict[str, Any] = {"enabled": body.enabled}
    if "tools" in body.model_fields_set:
        json_body["tools"] = body.tools
    result = await _nas_request(
        "PUT",
        portal_url,
        f"/api/portal/tools/toolkits/{slug}",
        token,
        json_body=json_body,
    )
    if isinstance(result, dict):
        normalized = dict(result)
        overrides = normalized.pop("toolOverrides", None)
        # NAS GET stores one override under each toolkit's `tools`, while PUT
        # returns the complete `toolOverrides` map; the dashboard uses one
        # `toolsOverride` field and must preserve absent versus empty scopes.
        normalized["toolsOverride"] = (
            overrides.get(slug) if isinstance(overrides, dict) else None
        )
        return normalized
    return result


@router.post("/api/capabilities/toolkits/{slug}/connect")
async def connect_toolkit(slug: str):
    """Proxy NAS's connect-account flow and return its connect URL."""
    portal_url, token = await _resolve_portal_auth()
    return await _nas_request(
        "POST",
        portal_url,
        f"/api/portal/tools/toolkits/{slug}/connect",
        token,
    )
