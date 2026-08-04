# adv_deleg4 — DELEG-4 adversarial re-derivation

Claim under test: for a tool slug that does not exist, `POST /v1/execute` returns
`{slug, successful:false}` with **no** `error`, while `POST /v1/schemas` returns
`TOOL_NOT_FOUND` for the same slug; `tools/tool_search.py:1112` then substitutes the
untyped placeholder `"the tool call failed"` into the model-visible tool result.

## Bridge half (deterministic, in-process)

```
cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge && \
  TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 .venv/bin/python -m pytest \
  tests/integration_tool_provider/adv_deleg4/test_unknown_slug_execute_error.py -q
```
2 passed. Test 1 pins the placeholder for the no-error shape; test 2 pins verbatim
passthrough for a real tool-level error, so the two failure classes are provably
indistinguishable only in the first case.

## Wire half (live gateway, one script)

```
tests/integration_tool_provider/adv_deleg4/probe_wire.sh
```
Mints a fresh user JWT from NAS, then runs five `/v1/execute` cases plus one
`/v1/schemas` case. Tokens are never printed.
