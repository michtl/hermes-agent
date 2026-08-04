# Spec — per-tool scoping, dashboard track (hermes-agent)

Local scratch spec, not for merge. Base document:
`tool-gateway/.worktrees/tool-provider-v1/docs/session-notes/SPEC-per-tool-scoping.md` — read that
first. This file is Track C's slice of the shared four-rule contract.

## The four rules (shared contract, do not reinterpret)

| Input | Meaning | Stored (NAS) |
|---|---|---|
| `tools` absent | no override — all tools of the toolkit allowed | override entry cleared |
| `tools: ["A","B"]` | narrow to exactly those slugs | `{ "<toolkit>": ["A","B"] }` |
| `tools: []` | deny all tools of the toolkit | `{ "<toolkit>": [] }` |
| `enabled: false` | toolkit off | removed from enabled list and override dropped |

`tools: []` is not the same as "absent" / "field not sent". Absent = unscoped (all tools allowed).
Empty array = scoped to nothing (deny-all). The dashboard's job is to let a user express both of
these distinguishably and never conflate them.

## Layer 1 — proxy (`hermes_cli/web_routers/capabilities.py`)

Currently 162 lines, 3 routes, all forwarding to NAS's `/api/portal/tools/*`.

1. `GET /api/capabilities/toolkits` — NAS's persistence layer (`src/server/composio/policy.ts` in
   the nous-account-service repo) stores this as `toolOverrides: Record<string, string[]> | null`
   keyed by toolkit slug (confirmed by reading that repo's policy code). The Track B work is
   expected to make the portal's toolkit-list GET surface each entry's override under some
   per-toolkit key — check what key Track B actually lands (read
   `src/app/api/portal/tools/toolkits/route.ts` in that repo, or its route test, once B has
   merged its change) rather than assuming. This proxy's GET handler already returns NAS's parsed
   JSON body untouched, so no server-side change is needed here unless the shape needs
   normalizing for the frontend — if so, normalize into a flat `toolsOverride: string[] | null`
   per toolkit entry (`null`/absent = unscoped, `[]` = deny-all) so the UI has one stable shape
   regardless of exactly how NAS nests it.

2. `PUT /api/capabilities/toolkits/{slug}` — extend `ToolkitEnabledUpdate` (in
   `hermes_cli/web_models.py`) with an optional `tools: Optional[List[str]] = None` field. Forward
   it to NAS only when the caller actually sent a `tools` key — i.e. distinguish "field absent from
   the incoming request" from "field present and set to `[]`". Pydantic's default `None` for an
   absent field is indistinguishable from an explicit `null`, so: give the API layer a way to tell
   "omit tools" from "tools is the empty list" — e.g. accept `tools: Optional[List[str]] = None`
   and only include `"tools"` in the JSON body forwarded to NAS when the request body actually
   contained the key (use `body.model_fields_set` or accept the raw dict and check membership).
   Do not silently convert `None` into "send tools: []" or vice versa — this is exactly the bug the
   base spec calls out.

3. `POST /api/capabilities/toolkits/{slug}/connect` — unchanged.

Preserve the existing timeout/error-passthrough style (400/401/403/404 pass through with upstream
detail; other failures become 502; NAS timeouts log a warning). Add proxy-level tests (extending
`tests/hermes_cli/test_web_server_capabilities.py`) for: PUT with `tools` omitted forwards no
`tools` key, PUT with `tools: []` forwards `tools: []` (not dropped), PUT with `tools: [...]`
forwards the list, and GET surfaces whatever override field NAS returns.

## Layer 2 — UI (`web/src/pages/CapabilitiesPage.tsx`, `web/src/lib/capability-catalog.ts`,
`web/src/lib/api.ts`)

### Types

- `web/src/lib/api.ts`: add the override field to `CapabilityToolkit` (match NAS's actual field
  name/shape — likely `toolsOverride: string[] | null` where `null`/absent = unscoped and `[]` =
  deny-all). Extend `setToolkitEnabled` — or add a new `setToolkitScope(slug, { enabled, tools })`
  — so the caller can send `tools` as `undefined` (omit), `[]` (deny-all), or a populated array,
  preserving the same three-way distinction through the fetch call (don't let `JSON.stringify`
  quietly drop `tools` when it's `undefined` vs. never include it if the caller means "clear the
  override" explicitly — those are two different UI actions, see below).
- `web/src/lib/capability-catalog.ts`: extend `MergedCapabilityToolkit` to carry the override field
  through `mergeCapabilityToolkit` untouched (pass-through, no enrichment needed for this field).

### Where it lives

In the existing detail slideover (`selectedToolkit && (...)` block, currently ~line 445-560), not
the catalog grid. Add a new section below "Recommended tools" (or wherever reads best next to the
existing Connect/Status row) titled something like "Tool scope". Reuse the slideover's existing
primitives: `H2 variant="sm"` section headers, `Badge`, `Button`, the same border/padding rhythm
(`border-t border-border pt-... `, `text-sm`/`text-xs text-muted-foreground` type scale) — do not
introduce new visual primitives.

### Input

A plain `<textarea>` (there is no existing textarea in this file — match the input styling used
elsewhere in the app's design system, e.g. the same border/background/focus treatment as other
form controls if any exist in shared UI components; otherwise a minimal bordered box consistent
with the page's `border-border`/`bg-card` language) for free-text tool slugs, one per line or
comma-separated. Parse permissively (split on newlines and commas, trim, drop empties, de-dupe).

Add an inline comment at the input explaining why there's no picker: NAS exposes a toolkit
catalog, not a tool catalog, so there is nothing to populate a picker from — this is a deliberate
scope cut, not an oversight, and a picker is a follow-up once a tool-listing endpoint exists.

### The three states — must be visually distinct

Compute a local `scopeState` from the toolkit's override field:

- `override == null` (absent): **"All tools allowed"** — unremarkable, e.g. default `Badge
  tone="outline"` or plain muted text. This is the default and must not look like a warning.
- `override` is a non-empty array of length N: **"Scoped to N tools"** — neutral/informational
  badge, e.g. `Badge tone="secondary"`, distinct from both the default and the blocked state.
- `override` is `[]` (empty array, explicitly saved): **"Scoped to none — all tools blocked"** —
  use `Badge tone="warning"` (already used elsewhere in this app, e.g. `WebhooksPage.tsx`,
  `SessionsPage.tsx`, for exactly this kind of "pay attention" state) plus a short explanatory line
  so the state cannot be mistaken for "no override."

### Two distinguishable save actions

The textarea has its own "dirty" state independent from "toolkit has no override." Provide two
explicit actions, not one ambiguous "Save":

- **Save scope** — parses the textarea's current content into a slug list and sends exactly that
  list (including `[]` if the textarea is empty but the user explicitly clicked Save) as `tools`.
  If the user clears the textarea to empty and clicks this, that is the deny-all action — surface
  a confirmation or a plainly-worded warning before submitting (a `confirm()`-style guard or an
  inline "this blocks all tools" notice the user must acknowledge), so saving empty is never an
  accident.
- **Clear override** (a separate, clearly labeled control, e.g. "Remove scoping" / "Allow all
  tools") — sends the PUT with no `tools` key at all, restoring "no override." Only enabled when an
  override currently exists.

These must not collapse into the same code path: "clear the field and save" (user's fingers) is a
different intent than "click Clear override" (explicit unscope), even though both could plausibly
end up sending different bodies. Concretely: emptying the textarea and clicking **Save scope**
sends `tools: []` (deny-all); clicking **Clear override** sends no `tools` key regardless of
textarea contents (unscope). Reset the textarea to reflect the fetched override whenever the
selected toolkit changes or after a successful save/clear (component effect keyed on
`selectedToolkit?.slug`).

### Round-trip

After a successful Save/Clear, refetch (or optimistically update local state from the response,
matching the existing `handleToggle` pattern of optimistic update + reconciliation with the
response) so the textarea and the state badge reflect what NAS actually stored, not just what was
submitted.

## Explicitly out of scope

Do not touch: `AdminTable`'s Soft-disable/Revoke stub controls (`CapabilitiesPage.tsx` around line
730, self-documented as prototype-only, never calls the API) or `AgentPreviewSidebar` ("Prototype —
not wired"). Both are stubs by design per the shared spec; leave them exactly as found.

## Gates

- `cd web && npm test` — 159 existing tests must stay green; add new tests covering: rendering each
  of the three scope states from a fetched toolkit, Save scope forwarding the parsed list
  (including the `[]` deny-all case with its warning), Clear override forwarding no `tools` key,
  and textarea reset when switching selected toolkit.
- `npx tsc -b` clean.
- `npm run build` clean.
- Python: proxy-layer tests in `tests/hermes_cli/test_web_server_capabilities.py` (run via
  `uv run pytest tests/hermes_cli/test_web_server_capabilities.py`), existing 78-test pytest suite
  must stay green.

## Note on the NAS field name/shape

This track does not implement NAS. If, when you write the proxy/UI code, the actual GET/PUT
response shape from NAS differs from the guesses above (field name, nesting, null vs. missing-key
semantics), match what NAS actually sends/expects and note the discrepancy in your final report
rather than silently picking a shape — the four rules are fixed, the wire field name is not.
