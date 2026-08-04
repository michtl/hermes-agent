#!/usr/bin/env python3
"""(d) ONE live tool_call dispatch of a hackernews gateway slug, end-to-end,
through model_tools.handle_function_call's real `tool_call` bridge path --
the same public dispatcher the live agent loop uses.

Proves real data flows through: model_tools.handle_function_call("tool_call",
{"name": <slug>, "arguments": {...}}) -> tools.tool_search.dispatch_provider_tool_call
-> tools.tool_provider_gateway.execute() -> POST {TOOLS_GATEWAY_URL}/v1/execute
-> back through the same call stack -> JSON result string.

hackernews needs no OAuth connection (see harness README), so this is a
real, unmocked round trip.

Usage (see README.md "Rerun" section for the full recipe):
    export HERMES_HOME=<scratch dir with providers.nous.access_token set>
    export HERMES_PORTAL_BASE_URL=http://127.0.0.1:3111
    export TOOLS_GATEWAY_URL=http://tools-gateway.localhost:3009
    cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge
    .venv/bin/python tests/integration_tool_provider/dispatch/live_hackernews_probe.py

Never pass the JWT on the command line or print it -- it only ever lives in
HERMES_HOME/auth.json (gitignored scratch dir) or the TOOL_GATEWAY_USER_TOKEN
env var, and this script does not print either.
"""
import json
import os
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, _REPO_ROOT)

from model_tools import handle_function_call  # noqa: E402
from tools.tool_backend_helpers import managed_nous_tools_enabled  # noqa: E402
from tools.tool_provider_gateway import resolve_tool_provider_gateway  # noqa: E402


def main() -> int:
    print("managed_nous_tools_enabled():", managed_nous_tools_enabled())
    cfg = resolve_tool_provider_gateway()
    print("gateway config resolved:", cfg is not None)
    if cfg is None:
        print("FAIL: gateway not entitled -- check HERMES_HOME/auth.json "
              "and that the minted JWT hasn't expired (900s TTL).")
        return 1
    print("  gateway_origin:", cfg.gateway_origin)
    print("  managed_mode:", cfg.managed_mode)
    print("  token present:", bool(cfg.nous_user_token), "len:", len(cfg.nous_user_token or ""))

    start = time.monotonic()
    result = handle_function_call(
        function_name="tool_call",
        function_args={
            "name": "HACKERNEWS_GET_USER",
            "arguments": {"username": "pg"},
        },
        task_id="live-dispatch-probe",
    )
    elapsed_ms = (time.monotonic() - start) * 1000

    print("\n--- result ---")
    print(result)
    print(f"\nelapsed_ms={elapsed_ms:.1f}")

    parsed = json.loads(result)
    if parsed.get("success") is not True:
        print(f"FAIL: expected success=True, got {parsed}")
        return 1
    if not parsed.get("data"):
        print("FAIL: expected non-empty data payload from HACKERNEWS_GET_USER")
        return 1
    print("\nOK: live gateway execute round-trip succeeded through "
          "handle_function_call('tool_call', ...)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
