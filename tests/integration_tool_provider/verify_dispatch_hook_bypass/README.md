# verify_dispatch_hook_bypass

Adversarial re-derivation of finding `dispatch-hook-bypass-1`, written from
scratch (does not reuse the finder's harness in `../dispatch/`).

Rerun:

```
cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge && \
TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 .venv/bin/python -m pytest \
  tests/integration_tool_provider/verify_dispatch_hook_bypass/test_verify_bypass.py -q -s
```

Expected (6 passed):

```
DIRECT-NAME stages: ['tool_request_middleware', 'pre_tool_call_hook',
                     'tool_execution_middleware', 'PROVIDER_DISPATCH',
                     'post_tool_call_hook', 'transform_tool_result_checked']
BRIDGE stages:      ['PROVIDER_DISPATCH']
CONTROL (feishu_doc_read) stages: ['tool_request_middleware:feishu_doc_read',
                                   'pre_tool_call_hook:feishu_doc_read']
```

Method: the seams are imported lazily inside `handle_function_call`, so they are
patched at their DEFINING modules (`hermes_cli.middleware`, `hermes_cli.plugins`),
not on `model_tools`.

Result: the bypass in `model_tools.py:1205-1210` is REAL but LATENT — every live
caller pre-unwraps the bridge before reaching it. See the verifier's report for
the reachability analysis (`agent/transports/hermes_tools_mcp_server.py`
dispatches an `EXPOSED_TOOLS` allowlist that excludes `tool_call`).
