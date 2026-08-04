# connections — app_connections + session-init + prompt injection + token expiry

Lens: app_connections status/connect shapes, the session-init `/v1/connections`
probe, the injected "External app tools" prompt line, and token-expiry
behavior of the tool-provider bridge client.

## Run everything (fast probes only, default)

```
cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge && \
TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 .venv/bin/python -m pytest \
tests/integration_tool_provider/connections/ -q
```

20 tests, all mocked (no network), ~3s. This is what CI / a normal rerun
should use.

## Run the live wire probe too (token expiry, real gateway call)

```
cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge && \
TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 .venv/bin/python -m pytest \
tests/integration_tool_provider/connections/ -q -m integration
```

4 additional tests in `test_token_expiry.py`, each making one real HTTP call
to the live gateway (`http://tools-gateway.localhost:3009`) with a
synthetically-expired HS256 JWT. No waiting for a real token to age out —
the token is crafted already-expired at construction time (signed with the
shared dev-mint secret, claim shape copied from a real decoded token). Runs
in ~1.3s.

Marked `integration` per this repo's existing convention
(`addopts = "-m 'not integration'"` in `pyproject.toml`) so it is excluded
from default full-suite runs and must be requested explicitly, same as any
other live-dependency test here.

## Files

- `test_app_connections_bounded.py` — status/connect shapes; proves the
  poll cadence named in `app_connections`'s returned `note`
  (`_POLL_MAX_ATTEMPTS=6`, `_POLL_INTERVAL_SECONDS=10`) is advisory text for
  the calling model, not an internal retry loop — a single dispatch is
  always exactly one gateway round-trip, even against a backend that never
  reports "connected"; asserts no vendor name in status/connect/error output.
- `test_session_init_probe.py` — pins the init contract (exactly one
  `/v1/connections {"toolkits": [], "action": "status"}` call,
  `context_id=None`) across zero/some/all connected; pins
  `agent/prompt_builder.py`'s injected "External app tools" line
  byte-for-byte for connected/none/probe-never-ran states (re-derived from
  current source, with a source-text guard so the pin can't silently drift);
  and proves three init failure modes (gateway down, 403 refusal, hung
  backend past the 8s `REQUEST_TIMEOUT_SECONDS`) all leave session init
  succeeding with `_tool_provider_connected_toolkits = None`, no crash, no
  vendor name, no traceback visible.
- `test_token_expiry.py` — the live wire probe described above, plus what
  the model sees (`app_connections` tool_error JSON) and what a fresh
  `init_agent()` call does when the connections probe itself hits a real
  401. See its module docstring for the full plain-English verdict.

## Findings (see also the orchestrator summary)

Everything here came back demo-safe: a never-connecting toolkit cannot hang
a single `app_connections` call (the retry loop lives in the calling model's
own tool-call cadence, not in this tool); the injected prompt line is exactly
what current source produces for each connected-state; all three init
failure modes degrade to `None` without crashing session start; and an
expired/invalid token gets a clean typed error (`AUTH_ERROR` / "Unauthorized")
at the gateway, the bridge client, the model-visible tool output, and the
init probe — never a vendor name, never a raw traceback, never a hang. The
one caveat worth flagging (not a demo blocker): there is no retry-on-401 or
reactive token refresh at the gateway-client layer — recovery from an
expired token is entirely the existing re-mint-and-relaunch operator step
(consistent with the already-known TTL/relaunch edge), not something this
lens's surfaces make worse.

## test_noauth_toolkit_connect.py — DELEG-5 adversarial re-derivation

`app_connections(action='connect')` on a **no-auth** toolkit (hackernews) is
deterministically broken at the gateway, independent of any model.

- `/v1/connections action=status` reports hackernews `disconnected` (HTTP 200)
  even though it needs no auth — that's the bait that invites a connect.
- `/v1/connections action=connect` on hackernews returns **HTTP 502
  `UPSTREAM_ERROR`, 3-for-3**. Control: `github` on the same token returns
  HTTP 200 + `connect_url`, so it is not auth/entitlement/token TTL.
- The bridge surfaces it as
  `{"error": "Upstream provider request failed", "code": "UPSTREAM_ERROR"}` —
  no hint that the toolkit needs no auth, indistinguishable from a transient
  fault, so an identical retry is locally rational.
- `app_connections` already has the correct "No new authorization was needed"
  branch (fires on a contract-shaped 200 with no `connect_url`); it is
  unreachable for no-auth toolkits because the gateway 502s first.

Root cause (READ-ONLY gateway reference,
`src/server/providers/composio/provider.ts` `connections()`): the `connect`
path unconditionally calls `getOrCreateManagedAuthConfigId(toolkit)`, which
falls through `authConfigs.list(...)` (0 items) to
`authConfigs.create(toolkit, {type:"use_composio_managed_auth"})`. Upstream
rejects that with HTTP 400 `Auth_Config_NoAuthApp`
("Cannot create an auth config for toolkit \"hackernews\" because it does not
require authentication"), and the route handler flattens the unhandled provider
error into a 502. There is no no-auth branch anywhere in the connect path.

NOT claimed here: that a model loops on the error (one transcript is not
evidence of a code defect), and the "client read-timeout" sub-claim did not
reproduce — all 4 live connect probes answered in ~1.2s.

Run (offline only, default):

    TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 .venv/bin/python -m pytest \
      tests/integration_tool_provider/connections/test_noauth_toolkit_connect.py -q

Run including live gateway probes:

    DELEG5_LIVE=1 TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 .venv/bin/python -m pytest \
      tests/integration_tool_provider/connections/test_noauth_toolkit_connect.py -q -s
