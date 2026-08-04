"""Model-directed connection checks and authorization-link creation."""

import json
import logging
import re
from typing import Any, Dict, List

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


def _resolve_gateway():
    try:
        from tools.tool_backend_helpers import managed_nous_tools_enabled

        if not managed_nous_tools_enabled():
            return None
        from tools.tool_provider_gateway import resolve_tool_provider_gateway

        return resolve_tool_provider_gateway()
    except Exception:
        logger.debug("manage_tool_connections gateway resolution failed", exc_info=True)
        return None


def check_manage_tool_connections_available() -> bool:
    return _resolve_gateway() is not None


def _clean_gateway_message(message: str) -> str:
    return re.sub(r"composio", "connection service", message, flags=re.IGNORECASE)


def _dispatch_manage_tool_connections(args: Dict[str, Any], **kw) -> str:
    raw_toolkits = args.get("toolkits", [])
    if not isinstance(raw_toolkits, list) or not all(
        isinstance(toolkit, str) and toolkit.strip() for toolkit in raw_toolkits
    ):
        return tool_error("toolkits must be a list of toolkit name strings")
    toolkits = [toolkit.strip() for toolkit in raw_toolkits]

    reinitiate = args.get("reinitiate", False)
    if not isinstance(reinitiate, bool):
        return tool_error("reinitiate must be true or false")

    gw_config = _resolve_gateway()
    if gw_config is None:
        return tool_error("External app connections are not available.")

    try:
        from model_tools import _run_async
        from tools.tool_provider_gateway import (
            ToolProviderGatewayError,
            ToolProviderTransportError,
            connections as _gw_connections,
        )

        results = _run_async(
            _gw_connections(
                gw_config,
                toolkits,
                "manage",
                context_id=None,
                reinitiate=reinitiate,
            )
        )
    except ToolProviderGatewayError as exc:
        return tool_error(_clean_gateway_message(exc.message), code=exc.code)
    except ToolProviderTransportError:
        return tool_error("Could not reach the external app connection service.")
    except Exception:
        logger.warning("manage_tool_connections dispatch failed", exc_info=True)
        return tool_error("Unexpected error managing external app connections.")

    connections_out: List[Dict[str, Any]] = []
    for connection in results:
        entry: Dict[str, Any] = {
            "toolkit": connection.toolkit,
            "status": connection.status,
        }
        if connection.connect_url:
            entry["connect_url"] = connection.connect_url
        if connection.account_hint:
            entry["account_hint"] = connection.account_hint
        if connection.note:
            entry["note"] = connection.note
        connections_out.append(entry)
    return json.dumps({"connections": connections_out}, ensure_ascii=False)


MANAGE_TOOL_CONNECTIONS_SCHEMA = {
    "name": "manage_tool_connections",
    "description": (
        "Check and manage the current user's connections for external app tools. "
        "Call this before using an app's tools when you are unsure whether its "
        "account is connected. Pass toolkit names from tool_search; omit toolkits "
        "or pass [] to survey every enabled toolkit. A pending result means the "
        "user must open the returned connect_url: surface that link to the user "
        "verbatim because it is short-lived, then wait and do not attempt the "
        "app's tools until a later call reports connected. Set reinitiate=true "
        "only when existing credentials seem stale and fresh authorization is needed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "toolkits": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Toolkit names from tool_search. Omit or pass an empty list to "
                    "check every toolkit enabled for the current user."
                ),
            },
            "reinitiate": {
                "type": "boolean",
                "description": (
                    "Force fresh authorization when an existing connection's "
                    "credentials appear stale. Defaults to false."
                ),
                "default": False,
            },
        },
    },
}

registry.register(
    name="manage_tool_connections",
    toolset="app_connections",
    schema=MANAGE_TOOL_CONNECTIONS_SCHEMA,
    handler=_dispatch_manage_tool_connections,
    check_fn=check_manage_tool_connections_available,
    requires_env=[],
    emoji="🔌",
)
