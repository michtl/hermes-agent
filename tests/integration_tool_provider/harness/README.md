# Local hermes harness

Stand up a fully configured, **isolated** hermes CLI against either the local
stack or a preview deployment, with one command and a doctor check.

Everything lives in two files:

| file | what it is |
|---|---|
| `targets.py` | the ONE config block. Adding a target is a few lines. |
| `hermes_harness.py` | the CLI: `targets` `probe` `up` `doctor` `run` `env` `down` |

It never touches the real `~/.hermes`. That directory is **read** once (for
`OPENROUTER_API_KEY`) and never written.

```
PY=/home/daimon/github/hermes-agent/.worktrees/composio-bridge/.venv/bin/python
H=/home/daimon/github/hermes-agent/.worktrees/composio-bridge/tests/integration_tool_provider/harness
```

---

## The one command per target

### local — works headless, end to end

```bash
$PY $H/hermes_harness.py up --target local --profile local && \
$PY $H/hermes_harness.py doctor --profile local
```

`up` mints a fresh token from NAS and writes the profile; `doctor` prints
PASS/FAIL per check. Then:

```bash
$PY $H/hermes_harness.py run --profile local "Reply with exactly: HARNESS_OK"
$PY $H/hermes_harness.py run --profile local --scenario bridge_connections.txt
```

### preview — needs a human-supplied token

```bash
$PY $H/hermes_harness.py up --target preview --profile preview --token "$JWT" && \
$PY $H/hermes_harness.py doctor --profile preview
```

Without `--token` this **fails loudly and never falls back to the dev route**.
See the caveat below.

### either target, no token, no profile

```bash
$PY $H/hermes_harness.py probe --target preview
```

`probe` checks target reachability and the URL shape without any credential. It
is how you verify a preview target is wired correctly before anyone hands you a
token.

---

## The preview caveats (both learned the hard way)

### 1. The gateway URL shape is NOT the same as local

The gateway routes to a provider via a host-based rewrite,
`{provider}-gateway.<domain>`. That rewrite **can never match a `*.vercel.app`
hostname**, so a preview must address the route **path-directly**:

| target | `TOOLS_GATEWAY_URL` | resulting `/v1/search` URL |
|---|---|---|
| local | `http://tools-gateway.localhost:3009` | `http://tools-gateway.localhost:3009/v1/search` |
| preview | `https://tool-gateway-git-sid-tool-provider-v1-nousresearch.vercel.app/api/passthrough/tools` | `…/api/passthrough/tools/v1/search` |

A path-prefixed origin is legal because hermes joins naively. In
`tools/tool_provider_gateway.py`:

```python
url = f"{config.gateway_origin.rstrip('/')}{path}"   # path == "/v1/search"
```

and `build_vendor_gateway_url()` returns `TOOLS_GATEWAY_URL` verbatim apart from
`.strip().rstrip("/")`. The env var name is **`TOOLS_GATEWAY_URL`** — with an S
— because hermes derives it as `f"{vendor.upper()}_GATEWAY_URL"` and the vendor
is `tools`. Get it wrong and you silently fall back to the production origin
instead of getting an error.

`targets.Target.gateway_url()` reproduces that exact join (deliberately *not*
`urljoin`, which would eat the `/api/passthrough/tools` prefix), and
`test_harness.py` pins it against hermes's own formula.

Measured, 2026-08-04:

```
path-direct  POST …/api/passthrough/tools/v1/connections  -> 401 {"error":{"code":"AUTH_ERROR",…}}   route reached
host-form    POST …/v1/connections                        -> 404 <!DOCTYPE html>…                    Vercel, no route
```

A **401 is the good answer** for an unauthenticated probe: it proves the URL
reached the provider handler. A 404 is what a wrong URL shape looks like.

### 2. Preview has NO headless token mint

`POST {NAS}/api/internal/dev-mint-oauth-token` is `NODE_ENV`-gated. Against the
preview it answers:

```
HTTP 404  {"error":"disabled_in_production"}
```

This is a **known open loop**, not a harness defect. The harness therefore
requires a token for preview and never silently tries the dev route:

```bash
--token <JWT>
--token-file <path>
HARNESS_TOKEN=<JWT>
```

To get one, either complete a real browser login against the preview and read
`providers.nous.access_token` out of that profile's `auth.json`, or have someone
with preview NAS env access mint one server-side.

---

## Token expiry is the top operational footgun

The token is baked into the process env at **launch** (900s TTL locally).
Refreshing `auth.json` under a **running** hermes does nothing — the process
already read it.

* `up` **re-mints by default** (pass `--no-remint` to reuse).
* `doctor` FAILs on an expired token and WARNs under 300s remaining.
* `run` refuses to start on an expired token.

**When a token expires: re-run `up`, then RELAUNCH. Do not refresh in place.**

---

## Profile toggles

Set at `up` time; they change what is demonstrable.

| flag | default | why it matters |
|---|---|---|
| `--subagent-auto-approve` | off (`false`) | The default **auto-DENIES** a child's dangerous-command approvals, which silently kills a delegate-to-gateway demo. Turn it on for delegation work. |
| `--max-spawn-depth N` | `1` | `1` = flat, no grandchildren. Raise to exercise nested delegation. |
| `--model ID` | `anthropic/claude-fable-5` | Any OpenRouter model id. |
| `--reasoning LEVEL` | `medium` | `none`…`max`. |
| `--max-turns N` | `60` | Agent turn cap. |
| `--profile NAME` | `default` | Several profiles coexist; each owns its own home and tmux session. |
| `--homes-root PATH` | `<harness>/.homes` | Where profiles live. Gitignored. |

---

## What `up` writes

Under `<homes-root>/<profile>/`:

```
hermes-home/auth.json     providers.nous.access_token + portal_base_url   (0600)
hermes-home/config.yaml   model, reasoning, delegation toggles
hermes-home/.env          OPENROUTER_API_KEY, copied from the real ~/.hermes/.env  (0600)
workdir/                  CWD for runs — NOT the repo root, so the repo's 75k
                          AGENTS.md does not dominate every transcript
profile.json              manifest: target, URLs, toggles, token fingerprint (no token)
token                     the raw JWT                                      (0600)
```

and exports into every launched process:

```
HERMES_HOME               the isolated profile
HERMES_PORTAL_BASE_URL    the trusted-operator escape hatch in hermes_cli/auth.py —
                          wins outright over the stored value AND bypasses
                          _NOUS_PORTAL_ALLOWED_HOSTS
TOOLS_GATEWAY_URL         per-target, see the table above
TOOL_GATEWAY_USER_TOKEN   the JWT
PYTHONPATH                the repo root
```

Entitlement is satisfied by **local decode** of the JWT's `paid_access` claim —
no network call. `doctor` surfaces `sub`, `aud`, `iss`, `exp` and `paid_access`,
and never the token or its signature.

---

## Transcript hygiene

`run` redacts on capture (JWTs, `sk-or-v1-`, `sk-ant-`, `sk-`, `ak_`/`oak_`,
plus the profile's own token and OpenRouter key as literals), then **re-greps its
own output** and refuses to finish if anything secret-shaped survives. Every
transcript ends with:

```
# redaction self-check: CLEAN
```

`.homes/` and `transcripts/` are gitignored. `profile.json` carries only a
`sha256:` fingerprint of the token, never the token.

> The self-check found a real bug in its own redactor during development:
> `\b[ao]k_` never matches `oak_…` (the `\b` only holds before the leading `o`,
> where `[ao]` consumes `o` and `k_` then fails against `a`). Fixed to
> `\b(?:oak|ak)_`. `test_harness.py::test_redacts_key_shapes` pins it.

---

## `run` modes

Default is **one-shot** (`hermes -z`): fast, scriptable, prints only the final
response. Prefer it.

```bash
$PY $H/hermes_harness.py run --profile local "your prompt"
$PY $H/hermes_harness.py run --profile local --scenario bridge_search.txt
```

`--tmux` drives the interactive CLI instead, for anything one-shot cannot show
(approval prompts, live TUI state):

```bash
$PY $H/hermes_harness.py run --profile local --tmux --until 'BRIDGE_OK' --keep "your prompt"
```

Scenario files live in `scenarios/` and are resolved by bare name.

**Steering note:** unsteered, the model curls public APIs instead of using the
bridge for no-auth asks. The shipped scenarios say "do NOT use curl, bash, or
the web" explicitly. Keep that in any scenario where the gateway path is the point.

---

## Cleanup

```bash
$PY $H/hermes_harness.py down --profile local
```

Deletes the profile directory and kills its tmux session. It refuses to delete
anything at or under the real `~/.hermes`.

---

## Offline tests

32 hermetic probes — no network, no live stack. They pin the URL joins (both
shapes), the env block, token decode/expiry, redaction, file modes, and the
"never dev-mint against a preview" rule.

```bash
cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge && \
  TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 .venv/bin/python -m pytest \
  tests/integration_tool_provider/harness/test_harness.py -q
```

---

## Worked example (real output, local, 2026-08-04)

```
$ $PY $H/hermes_harness.py up --target local --profile local
profile   local
target    local
home      …/harness/.homes/local/hermes-home
workdir   …/harness/.homes/local/workdir
token     dev-mint  sha256:66c40863d577
          sub=nas_user:f7141b46-b044-41b0-aa13-a36a66f64f26
          aud=hermes-cli:hermes-agent  paid_access=True
          exp=2026-08-04T19:45:34+00:00  (899s left)
model     anthropic/claude-fable-5  (reasoning=medium)
toggles   subagent_auto_approve=False  max_spawn_depth=1
openrouter key from /home/daimon/.hermes/.env

$ $PY $H/hermes_harness.py doctor --profile local
  [PASS] profile file auth.json
  [PASS] profile file config.yaml
  [PASS] isolation — HERMES_HOME is not /home/daimon/.hermes
  [PASS] OPENROUTER_API_KEY — source: /home/daimon/.hermes/.env
  [PASS] token decodes — sha256:66c40863d577 sub=nas_user:f7141b46-…
  [INFO] token aud — hermes-cli:hermes-agent
  [INFO] token iss — http://127.0.0.1:3111
  [PASS] token unexpired — 895s left (exp 2026-08-04T19:45:34+00:00)
  [PASS] entitlement claim — paid_access=True (decoded locally, no network call)
  [PASS] NAS reachable — http://127.0.0.1:3111 -> HTTP 200
  [INFO] gateway URL join — host: http://tools-gateway.localhost:3009/v1/connections
  [PASS] gateway route exists — unauthenticated POST -> HTTP 401
  [PASS] /v1/connections round-trip — HTTP 200, 6 toolkit(s): hackernews, github,
         googlecalendar, gmail, notion, slack
  [INFO] connected toolkits — none (connect wall)
doctor: PASS
```

---

## Notes for whoever uses this next

* The canonical seeded identity is baked into `targets.py`. A fresh/synthetic
  userId has no `OrgMembership` row, so NAS answers 403 `org_access_denied` and
  the gateway fails closed to an **empty toolkit set** — which looks exactly
  like a bug and is not. Use the seeded user for anything needing a real policy.
* `hackernews` needs no auth and is the toolkit to use for real executions.
  Note it still reports `status: disconnected` in `/v1/connections`.
* Google/GitHub OAuth completion is a human step. The connect wall is the
  accepted stopping line.
* `app_connections` rejects an **empty** toolkit list even though the gateway's
  `/v1/connections` accepts `{"toolkits": []}` and answers with all six. Name
  the toolkits explicitly in scenarios.
* **The default workdir path contains the worktree name, and the agent's CWD is
  model-visible.** This worktree is named after the upstream vendor, so a run
  launched from the default `.homes/` root puts that name in the model's
  context via its own CWD. If a run must not expose it, put the profile
  somewhere neutral:

  ```bash
  $PY $H/hermes_harness.py up --target local --profile local --homes-root /tmp/hh
  ```

  Everything else the harness emits — doctor lines, transcript headers, the env
  block — is already vendor-free.
