#!/usr/bin/env node
/**
 * Second half of the dashboard round-trip lens (STATE-MUTATING).
 *
 * The first probe (roundtrip.mjs) proves the enable/disable bit round-trips.
 * This one asks the follow-up question that a control plane lives or dies by:
 * does an enable/disable round-trip PRESERVE the rest of the toolkit's policy?
 *
 * Sequence, all through the real UI:
 *   1. Save a 2-tool scope for the toolkit ("Tool slugs" textarea + Save scope)
 *      -> assert the DB persists toolOverrides
 *   2. Record what the gateway does with that scope (evidence only)
 *   3. Toggle the toolkit OFF, then back ON  (the exact thing an admin does when
 *      they want to briefly cut access)
 *   4. Assert whether the scope survived
 *
 * Also checks that a disabled toolkit is refused at /v1/schemas, not just
 * absent from /v1/connections — i.e. the disable is enforcement, not cosmetics.
 */
import { chromium } from "playwright";
import { readFileSync, writeFileSync } from "node:fs";
import { execSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const BASE_URL = process.env.BASE_URL ?? "http://127.0.0.1:8092";
const SESSION_TOKEN = process.env.DASH_SESSION_TOKEN ?? "";
const GW_URL = process.env.GW_URL ?? "http://tools-gateway.localhost:3009";
const NAS_URL = process.env.NAS_URL ?? "http://127.0.0.1:3111";
const WARM = readFileSync(process.env.WARM_TOKEN_FILE, "utf8").trim();
const COLD = readFileSync(process.env.COLD_TOKEN_FILE, "utf8").trim();
const SLUG = process.env.TOOLKIT_SLUG ?? "notion";
const NAME = process.env.TOOLKIT_NAME ?? "Notion";
const DB = process.env.DB_QUERY_CMD ?? "";
const ARTIFACT_DIR = fileURLToPath(new URL("./artifacts/", import.meta.url));

const IN_SCOPE = "NOTION_FETCH_DATA";
const OUT_OF_SCOPE = "NOTION_CREATE_COMMENT";
const SCOPE = [IN_SCOPE, "NOTION_SEARCH_NOTION_PAGE"];

const results = { checks: [], findings: [], evidence: {} };
function check(name, pass, evidence) {
  results.checks.push({ name, pass, evidence });
  console.log(`[${pass ? "PASS" : "FAIL"}] ${name} :: ${evidence}`);
}
function finding(severity, summary, evidence) {
  results.findings.push({ severity, summary, evidence });
  console.log(`[FINDING:${severity}] ${summary} :: ${evidence}`);
}

function dbRow() {
  const sql =
    'select row_to_json(t) from (select "enabledToolkits","toolOverrides" from "OrgToolkitPolicy") t;';
  return JSON.parse(execSync(`${DB} ${JSON.stringify(sql)}`, { encoding: "utf8" }).trim());
}
async function schemas(token, slugs) {
  const r = await fetch(`${GW_URL}/v1/schemas`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify({ tool_slugs: slugs }),
  });
  const b = await r.json().catch(() => null);
  return { status: r.status, got: (b?.schemas ?? []).map((s) => s.slug), code: b?.error?.code };
}
async function nasEntry(token) {
  const r = await fetch(`${NAS_URL}/api/portal/tools/toolkits`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const b = await r.json();
  return b.toolkits.find((t) => t.slug === SLUG);
}

async function openDetailPanel(page) {
  await page.goto(`${BASE_URL}/capabilities`, { waitUntil: "networkidle" });
  await page.getByRole("textbox", { name: "Search toolkits" }).fill(NAME);
  await page.getByText(NAME, { exact: true }).first().click();
  await page.getByRole("textbox", { name: "Tool slugs" }).waitFor({ timeout: 15000 });
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  if (SESSION_TOKEN) await ctx.setExtraHTTPHeaders({ "X-Hermes-Session-Token": SESSION_TOKEN });
  const page = await ctx.newPage();

  // ---- 1. save a scope through the UI --------------------------------------
  await openDetailPanel(page);
  const savePut = page.waitForResponse(
    (r) => r.request().method() === "PUT" && r.url().includes(`/toolkits/${SLUG}`),
    { timeout: 20000 },
  );
  await page.getByRole("textbox", { name: "Tool slugs" }).fill(SCOPE.join("\n"));
  await page.getByRole("button", { name: "Save scope" }).click();
  const saveResp = await savePut;
  const saveBody = await saveResp.json().catch(() => null);
  check(
    "UI: Save scope issued PUT with the tool list and got 200",
    saveResp.status() === 200,
    `HTTP ${saveResp.status()} body=${JSON.stringify(saveBody)}`,
  );
  await page.screenshot({ path: `${ARTIFACT_DIR}10-scope-saved.png` });

  const dbScoped = dbRow();
  results.evidence.dbAfterScopeSave = dbScoped;
  check(
    "DB persists the tool scope in toolOverrides",
    JSON.stringify(dbScoped.toolOverrides?.[SLUG]) === JSON.stringify(SCOPE),
    `toolOverrides=${JSON.stringify(dbScoped.toolOverrides)}`,
  );
  const nasScoped = await nasEntry(WARM);
  // NAS catalog entries carry a `logo` URL on the vendor's CDN host. Captured
  // artifacts get committed, and the vendor literal must not appear in them, so
  // drop that field from the evidence (it is already a known, separately
  // reported leak in the live response body itself).
  const { logo: _logo, ...nasScopedClean } = nasScoped ?? {};
  results.evidence.nasAfterScopeSave = nasScopedClean;
  check(
    "NAS GET echoes the tool scope back",
    JSON.stringify(nasScoped?.tools) === JSON.stringify(SCOPE),
    `nas entry tools=${JSON.stringify(nasScoped?.tools)}`,
  );

  // ---- 2. what does the gateway do with the scope? (evidence) --------------
  // COLD principal: no cache entry, so this reads the just-saved policy.
  const scopedOut = await schemas(COLD, [OUT_OF_SCOPE]);
  const scopedIn = await schemas(COLD, [IN_SCOPE]);
  results.evidence.gatewayScopeEnforcement = { inScope: scopedIn, outOfScope: scopedOut };
  console.log(
    `[INFO] gateway /v1/schemas with a 2-tool scope active: in-scope ${IN_SCOPE} -> HTTP ${scopedIn.status} ${JSON.stringify(scopedIn.got)}; ` +
      `out-of-scope ${OUT_OF_SCOPE} -> HTTP ${scopedOut.status} ${JSON.stringify(scopedOut.got)}`,
  );

  // ---- 3. toggle OFF then ON through the UI -------------------------------
  await page.goto(`${BASE_URL}/capabilities`, { waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "admin" }).click();
  await page.getByRole("textbox", { name: "Search toolkits" }).fill(NAME);

  const offPut = page.waitForResponse(
    (r) => r.request().method() === "PUT" && r.url().includes(`/toolkits/${SLUG}`),
    { timeout: 20000 },
  );
  await page.getByRole("switch", { name: `Disable ${NAME}`, exact: true }).click();
  await offPut;
  await page.getByRole("switch", { name: `Enable ${NAME}`, exact: true }).waitFor({ timeout: 10000 });
  const dbOff = dbRow();
  results.evidence.dbAfterDisable = dbOff;

  // disabled toolkit must be refused at /v1/schemas, not merely hidden
  const coldWhileOff = await (async () => {
    // the cold principal is warm by now; wait out the 30s TTL once
    await new Promise((r) => setTimeout(r, 31_000));
    return schemas(COLD, [IN_SCOPE]);
  })();
  check(
    "GW refuses /v1/schemas for a tool in a DISABLED toolkit (enforcement, not cosmetics)",
    coldWhileOff.got.length === 0,
    `HTTP ${coldWhileOff.status} code=${coldWhileOff.code} schemas=${JSON.stringify(coldWhileOff.got)}`,
  );

  const onPut = page.waitForResponse(
    (r) => r.request().method() === "PUT" && r.url().includes(`/toolkits/${SLUG}`),
    { timeout: 20000 },
  );
  await page.getByRole("switch", { name: `Enable ${NAME}`, exact: true }).click();
  await onPut;
  await page.getByRole("switch", { name: `Disable ${NAME}`, exact: true }).waitFor({ timeout: 10000 });

  // ---- 4. did the scope survive the round-trip? ---------------------------
  const dbBack = dbRow();
  results.evidence.dbAfterReenable = dbBack;
  const scopeSurvived = JSON.stringify(dbBack.toolOverrides?.[SLUG]) === JSON.stringify(SCOPE);
  check(
    "tool scope survives a UI disable -> re-enable round-trip",
    scopeSurvived,
    `before=${JSON.stringify(SCOPE)} after=${JSON.stringify(dbBack.toolOverrides)}`,
  );
  if (!scopeSurvived) {
    finding(
      "high",
      "A UI disable -> re-enable round-trip silently DESTROYS the toolkit's saved tool scope, widening access from N tools to all tools with no warning",
      `Saved scope ${JSON.stringify(SCOPE)} for '${SLUG}' through the dashboard; toggled the toolkit off and back on through the same page; ` +
        `OrgToolkitPolicy.toolOverrides is now ${JSON.stringify(dbBack.toolOverrides)}. ` +
        `Cause: NAS setToolkitEnabled() does 'delete toolOverrides[slug]' when enabled=false OR when tools is undefined, and the dashboard's ` +
        `handleToggle() calls api.setToolkitEnabled(slug, enabled) with NO tools field, so BOTH legs of the round-trip clear it. ` +
        `The UI never warns, and the re-enabled toolkit silently reverts to all-tools.`,
    );
  }
  // and what the agent can now reach
  await new Promise((r) => setTimeout(r, 31_000));
  const afterOut = await schemas(COLD, [OUT_OF_SCOPE]);
  results.evidence.gatewayAfterRoundTrip = afterOut;
  console.log(
    `[INFO] after the round-trip, previously OUT-OF-SCOPE ${OUT_OF_SCOPE} -> HTTP ${afterOut.status} ${JSON.stringify(afterOut.got)}`,
  );

  // ---- UI honesty: reload shows the (destroyed) scope, not a stale draft ---
  await openDetailPanel(page);
  const shownScope = await page.getByRole("textbox", { name: "Tool slugs" }).inputValue();
  check(
    "UI after reload shows the real post-round-trip scope (empty), not a stale draft",
    shownScope.trim() === "",
    `textarea=${JSON.stringify(shownScope)}`,
  );
  await page.screenshot({ path: `${ARTIFACT_DIR}11-scope-after-roundtrip.png` });

  await browser.close();
  writeFileSync(`${ARTIFACT_DIR}scope_results.json`, JSON.stringify(results, null, 2));
  const failed = results.checks.filter((c) => !c.pass);
  console.log(
    `\n== ${results.checks.length - failed.length}/${results.checks.length} checks passed, ${results.findings.length} finding(s) ==`,
  );
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => {
  console.error("FATAL", e);
  writeFileSync(`${ARTIFACT_DIR}scope_results.json`, JSON.stringify(results, null, 2));
  process.exit(2);
});
