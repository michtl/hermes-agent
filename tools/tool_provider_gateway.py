"""Typed async client for the managed Nous tool-provider bridge.

The bridge is described in ``docs/design/tool-provider-bridge.md``. Its wire
contract is owned by the tool-gateway repository on branch
``sid/tool-provider-v1`` in ``docs/tool-provider-v1-contract.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from tools.managed_tool_gateway import ManagedToolGatewayConfig, resolve_managed_tool_gateway

logger = logging.getLogger(__name__)

TOOL_PROVIDER_VENDOR = "tools"
REQUEST_TIMEOUT_SECONDS = 8.0


class ToolProviderGatewayError(RuntimeError):
    """A structured ApiError the gateway returned (subscription, toolkit, etc.)."""

    def __init__(self, code: str, message: str, toolkits: Optional[List[str]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.toolkits = list(toolkits or [])


class ToolProviderTransportError(RuntimeError):
    """Network/timeout/unreadable-response failure (not a gateway refusal)."""


@dataclass(frozen=True)
class ToolRef:
    slug: str
    toolkit: str
    description: str = ""
    connected: bool = False
    input_schema: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class SearchResultGroup:
    use_case: str
    tools: List[ToolRef] = field(default_factory=list)
    plan: List[str] = field(default_factory=list)
    pitfalls: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProviderSearchResponse:
    context_id: Optional[str]
    results: List[SearchResultGroup] = field(default_factory=list)
    connections: List[Dict[str, Any]] = field(default_factory=list)
    guidance: List[str] = field(default_factory=list)

    def all_tools(self) -> List["ToolRef"]:
        """Flatten every result group's tools into one ordered list."""
        out: List[ToolRef] = []
        for result in self.results:
            out.extend(result.tools)
        return out


@dataclass(frozen=True)
class ProviderSchema:
    slug: str
    toolkit: str
    description: str
    input_schema: Dict[str, Any]
    output_schema: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class ProviderExecuteResult:
    slug: str
    successful: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class ProviderExecuteResponse:
    context_id: Optional[str]
    results: List[ProviderExecuteResult] = field(default_factory=list)


@dataclass(frozen=True)
class ProviderConnection:
    toolkit: str
    status: str
    connect_url: Optional[str] = None
    account_hint: Optional[str] = None
    note: Optional[str] = None


def resolve_tool_provider_gateway() -> Optional[ManagedToolGatewayConfig]:
    """Resolve gateway config for the 'tools' vendor, if available."""
    return resolve_managed_tool_gateway(TOOL_PROVIDER_VENDOR)


def is_tool_provider_entitled() -> bool:
    return resolve_tool_provider_gateway() is not None


# --- internal transport --------------------------------------------------


def _headers(config: ManagedToolGatewayConfig) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {config.nous_user_token}",
        "Content-Type": "application/json",
    }


async def _post(
    config: ManagedToolGatewayConfig,
    path: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Make one POST, raising typed gateway or transport errors."""
    import httpx

    url = f"{config.gateway_origin.rstrip('/')}{path}"
    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=_headers(config), json=payload)
    except Exception as exc:
        raise ToolProviderTransportError(f"{type(exc).__name__}: {exc}") from exc

    try:
        body = response.json()
    except Exception:
        body = None

    if response.status_code >= 400:
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = body["error"]
            raise ToolProviderGatewayError(
                code=str(error.get("code") or "UPSTREAM_ERROR"),
                message=str(
                    error.get("message")
                    or f"tools-gateway refused the request (HTTP {response.status_code})"
                ),
                toolkits=(
                    error.get("toolkits")
                    if isinstance(error.get("toolkits"), list)
                    else None
                ),
            )
        raise ToolProviderTransportError(
            f"tools-gateway returned HTTP {response.status_code} with an unreadable body"
        )

    if not isinstance(body, dict):
        raise ToolProviderTransportError("tools-gateway returned a non-JSON response body")
    return body


def _optional_string(value: Any) -> Optional[str]:
    return str(value) if value is not None else None


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


# --- public endpoint functions ------------------------------------------


async def search(
    config: ManagedToolGatewayConfig,
    queries: List[str],
    *,
    context_id: Optional[str] = None,
    model: Optional[str] = None,
) -> ProviderSearchResponse:
    payload: Dict[str, Any] = {}
    if queries:
        payload["queries"] = queries
    if context_id:
        payload["context_id"] = context_id
    if model:
        payload["model"] = model

    body = await _post(config, "/v1/search", payload)
    parsed_results: List[SearchResultGroup] = []
    raw_results = body.get("results")
    if isinstance(raw_results, list):
        for raw_group in raw_results:
            if not isinstance(raw_group, dict):
                continue
            parsed_tools: List[ToolRef] = []
            raw_tools = raw_group.get("tools")
            if isinstance(raw_tools, list):
                for raw_tool in raw_tools:
                    if not isinstance(raw_tool, dict):
                        continue
                    input_schema = raw_tool.get("input_schema")
                    parsed_tools.append(
                        ToolRef(
                            slug=str(raw_tool.get("slug") or ""),
                            toolkit=str(raw_tool.get("toolkit") or ""),
                            description=str(raw_tool.get("description") or ""),
                            connected=bool(raw_tool.get("connected")),
                            input_schema=input_schema if isinstance(input_schema, dict) else None,
                        )
                    )
            parsed_results.append(
                SearchResultGroup(
                    use_case=str(raw_group.get("use_case") or ""),
                    tools=parsed_tools,
                    plan=_string_list(raw_group.get("plan")),
                    pitfalls=_string_list(raw_group.get("pitfalls")),
                )
            )

    raw_connections = body.get("connections")
    parsed_connections = (
        [dict(item) for item in raw_connections if isinstance(item, dict)]
        if isinstance(raw_connections, list)
        else []
    )
    return ProviderSearchResponse(
        context_id=_optional_string(body.get("context_id")),
        results=parsed_results,
        connections=parsed_connections,
        guidance=_string_list(body.get("guidance")),
    )


async def schemas(
    config: ManagedToolGatewayConfig,
    slugs: List[str],
    *,
    context_id: Optional[str] = None,
    include_output: bool = False,
) -> List[ProviderSchema]:
    payload: Dict[str, Any] = {"tool_slugs": slugs}
    if context_id:
        payload["context_id"] = context_id
    if include_output:
        payload["include_output"] = True

    body = await _post(config, "/v1/schemas", payload)
    parsed: List[ProviderSchema] = []
    raw_schemas = body.get("schemas")
    if not isinstance(raw_schemas, list):
        return parsed
    for raw_schema in raw_schemas:
        if not isinstance(raw_schema, dict):
            continue
        input_schema = raw_schema.get("input_schema")
        output_schema = raw_schema.get("output_schema")
        parsed.append(
            ProviderSchema(
                slug=str(raw_schema.get("slug") or ""),
                toolkit=str(raw_schema.get("toolkit") or ""),
                description=str(raw_schema.get("description") or ""),
                input_schema=input_schema if isinstance(input_schema, dict) else {},
                output_schema=output_schema if isinstance(output_schema, dict) else None,
            )
        )
    return parsed


async def execute(
    config: ManagedToolGatewayConfig,
    tools: List[Dict[str, Any]],
    *,
    context_id: Optional[str] = None,
    thought: Optional[str] = None,
) -> ProviderExecuteResponse:
    payload: Dict[str, Any] = {"tools": tools}
    if context_id:
        payload["context_id"] = context_id
    if thought:
        payload["thought"] = thought

    body = await _post(config, "/v1/execute", payload)
    parsed: List[ProviderExecuteResult] = []
    raw_results = body.get("results")
    if isinstance(raw_results, list):
        for raw_result in raw_results:
            if not isinstance(raw_result, dict):
                continue
            data = raw_result.get("data")
            parsed.append(
                ProviderExecuteResult(
                    slug=str(raw_result.get("slug") or ""),
                    successful=bool(raw_result.get("successful")),
                    data=data if isinstance(data, dict) else None,
                    error=_optional_string(raw_result.get("error")),
                )
            )
    return ProviderExecuteResponse(
        context_id=_optional_string(body.get("context_id")),
        results=parsed,
    )


async def connections(
    config: ManagedToolGatewayConfig,
    toolkits: List[str],
    action: str,
    *,
    context_id: Optional[str] = None,
    reinitiate: bool = False,
) -> List[ProviderConnection]:
    payload: Dict[str, Any] = {"toolkits": toolkits, "action": action}
    if action == "manage":
        payload["reinitiate"] = reinitiate
    if context_id:
        payload["context_id"] = context_id

    body = await _post(config, "/v1/connections", payload)
    parsed: List[ProviderConnection] = []
    raw_connections = body.get("connections")
    if not isinstance(raw_connections, list):
        return parsed
    for raw_connection in raw_connections:
        if not isinstance(raw_connection, dict):
            continue
        parsed.append(
            ProviderConnection(
                toolkit=str(raw_connection.get("toolkit") or ""),
                status=str(raw_connection.get("status") or ""),
                connect_url=_optional_string(raw_connection.get("connect_url")),
                account_hint=_optional_string(raw_connection.get("account_hint")),
                note=_optional_string(raw_connection.get("note")),
            )
        )
    return parsed


__all__ = [
    "TOOL_PROVIDER_VENDOR",
    "REQUEST_TIMEOUT_SECONDS",
    "ToolProviderGatewayError",
    "ToolProviderTransportError",
    "ToolRef",
    "SearchResultGroup",
    "ProviderSearchResponse",
    "ProviderSchema",
    "ProviderExecuteResult",
    "ProviderExecuteResponse",
    "ProviderConnection",
    "resolve_tool_provider_gateway",
    "is_tool_provider_entitled",
    "search",
    "schemas",
    "execute",
    "connections",
]
