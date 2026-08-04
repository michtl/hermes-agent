#!/usr/bin/env node
/**
 * Dashboard toggle round-trip lens (STATE-MUTATING).
 *
 * Toggles ONE toolkit (`notion`) OFF and back ON through the real dashboard UI
 * (Playwright click on the Switch — never a raw API call), and measures how the
 * change lands on three independent surfaces:
 *
 *   UI  -> the rendered Capabilities page (incl. a full reload, to prove the
 *          state is persisted and not React-local optimistic state)
 *   NAS -> GET  {NAS}/api/portal/tools/toolkits            (control plane read)
 *   DB  -> the OrgToolkitPolicy row in postgres            (source of truth)
 *   GW  -> POST {GATEWAY}/v1/connections                   (what the agent can do)
 *
 * The gateway leg is the point of the whole exercise: a UI toggle must actually
 * change the agent's reachable tool surface.
 *
 * COLD vs WARM principal — the method note that makes this measurement real:
 * the gateway's policy cache (src/server/providers/composio/policy.ts) is an
 * in-process Map keyed by *principalId* with a 30s TTL. A fresh token for the
 * SAME principal therefore hits the SAME warm cache entry. A genuinely cold
 * measurement needs a DIFFERENT principal — and that principal must be a real
 * member of the same org, because a non-member gets NAS 403 and the gateway
 * fails closed to an empty toolkit set, which is indistinguishable from a real
 * disable and would silently fake a PASS.
 *
 * Run: see README.md (run.sh drives this).
 */
import { chromium } from "playwright";
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

const BASE_URL = process.env.BASE_URL ?? "http://127.0.0.1:8092";
const SESSION_TOKEN = process.env.DASH_SESSION_TOKEN ?? "";
const NAS_URL = process.env.NAS_URL ?? "http://127.0.0.1:3111";
const GW_URL = process.env.GW_URL ?? "http://tools-gateway.localhost:3009";
const WARM_TOKEN_FILE = process.env.WARM_TOKEN_FILE ?? "";
const COLD_TOKEN_FILE = process.env.COLD_TOKEN_FILE ?? "";
const SLUG = process.env.TOOLKIT_SLUG ?? "notion";
const NAME = process.env.TOOLKIT_NAME ?? "Notion";
const ARTIFACT_DIR = fileURLToPath(new URL("./artifacts/", import.meta.url));
const DB = process.env.DB_QUERY_CMD ?? "";

const WARM = readFileSync(WARM_TOKEN_FILE, "utf8").trim();
const COLD = readFileSync(COLD_TOKEN_FILE, "utf8").trim();

const results = { checks: [], findings: [], timings: {}, states: {} };
const t0 = Date.now();
const el = () => `${((Date.now() - t0) / 1000).toFixed(2)}s`;
function check(name, pass, evidence) {
  results.checks.push({ name, pass, evidence });
  console.log(`[${pass ? "PASS" : "FAIL"}] ${name} :: ${evidence}`);
}
function finding(severity, summary, evidence) {
  results.findings.push({ severity, summary, evidence });
  console.log(`[FINDING:${severity}] ${summary} :: ${evidence}`);
}
function note(msg) {
  console.log(`[..${el()}] ${msg}`);
}

// ---------------------------------------------------------------- surfaces --
async function nasEnabled(token) {
  const r = await fetch(`${NAS_URL}/api/portal/tools/toolkits`, {
    headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
  });
  const b = await r.json();
  if (!Array.isArray(b?.toolkits)) return { status: r.status, error: b, enabled: null };
  return {
    status: r.status,
    enabled: b.toolkits.filter((t) => t.enabled).map((t) => t.slug).sort(),
    entry: b.toolkits.find((t) => t.slug === SLUG) ?? null,
  };
}

/** POST /v1/connections with an empty toolkit list => "everything enabled". */
async function gwEnabled(token) {
  const started = Date.now();
  const r = await fetch(`${GW_URL}/v1/connections`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "content-type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify({ toolkits: [], action: "status" }),
  });
  const b = await r.json().catch(() => null);
  const conns = Array.isArray(b?.connections) ? b.connections : null;
  return {
    status: r.status,
    completedAt: Date.now(),
    latencyMs: Date.now() - started,
    toolkits: conns ? conns.map((c) => c.toolkit).sort() : null,
    raw: b,
  };
}

async function dbRow() {
  if (!DB) return null;
  const { execSync } = await import("node:child_process");
  const sql =
    'select row_to_json(t) from (select "orgId","enabledToolkits","toolOverrides" from "OrgToolkitPolicy") t;';
  const out = execSync(`${DB} ${JSON.stringify(sql)}`, { encoding: "utf8" }).trim();
  return out ? JSON.parse(out) : null;
}

// --------------------------------------------------------------------- run --
async function main() {
  // ---- (1) baseline on all three surfaces -----------------------------------
  const nas0 = await nasEnabled(WARM);
  const db0 = await dbRow();
  results.states.baseline = { nas: nas0.enabled, db: db0 };
  console.log("BASELINE db row:", JSON.stringify(db0));
  console.log("BASELINE nas enabled:", JSON.stringify(nas0.enabled));
  check(
    "baseline: target toolkit is enabled on NAS + DB",
    nas0.enabled?.includes(SLUG) && db0?.enabledToolkits?.includes(SLUG),
    `nas=${nas0.enabled?.includes(SLUG)} db=${db0?.enabledToolkits?.includes(SLUG)}`,
  );
  writeFileSync(`${ARTIFACT_DIR}baseline.json`, JSON.stringify(results.states.baseline, null, 2));

  // ---- warm the gateway cache for the WARM principal, BEFORE the change ----
  const warm0 = await gwEnabled(WARM);
  const warmFetchCompletedAt = warm0.completedAt;
  check(
    "baseline: gateway sees target toolkit (warm principal)",
    warm0.status === 200 && warm0.toolkits?.includes(SLUG),
    `HTTP ${warm0.status} toolkits=${JSON.stringify(warm0.toolkits)}`,
  );
  note(`warm principal cache seeded at t+0; 30s TTL clock started`);

  // ---- (2) DISABLE through the UI ------------------------------------------
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  if (SESSION_TOKEN) {
    await ctx.setExtraHTTPHeaders({ "X-Hermes-Session-Token": SESSION_TOKEN });
  }
  const page = await ctx.newPage();
  const consoleErrors = [];
  page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
  const putCalls = [];
  page.on("response", (r) => {
    if (r.request().method() === "PUT" && r.url().includes("/api/capabilities/toolkits/")) {
      putCalls.push({ url: r.url(), status: r.status(), at: Date.now() });
    }
  });

  await page.goto(`${BASE_URL}/capabilities`, { waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "admin" }).click();
  await page.getByRole("textbox", { name: "Search toolkits" }).fill(NAME);
  const disableSwitch = page.getByRole("switch", { name: `Disable ${NAME}`, exact: true });
  await disableSwitch.waitFor({ state: "visible", timeout: 15000 });
  check("UI: switch renders as ON before the toggle", true, `aria-label="Disable ${NAME}" present`);
  await page.screenshot({ path: `${ARTIFACT_DIR}01-before-disable.png` });

  const putOffPromise = page.waitForResponse(
    (r) => r.request().method() === "PUT" && r.url().includes(`/toolkits/${SLUG}`),
    { timeout: 20000 },
  );
  await disableSwitch.click();
  const putOff = await putOffPromise;
  const T0 = Date.now(); // change committed by the control plane
  const putOffBody = await putOff.json().catch(() => null);
  results.timings.T0_disable_put_ms_since_start = T0 - t0;
  check(
    "UI: disable click issued PUT enabled=false and got 200",
    putOff.status() === 200 && putOffBody?.enabled === false,
    `HTTP ${putOff.status()} body=${JSON.stringify(putOffBody)}`,
  );

  await page.getByRole("switch", { name: `Enable ${NAME}`, exact: true }).waitFor({ timeout: 10000 });
  const disabledBadge = await page
    .locator("tr", { hasText: NAME })
    .first()
    .innerText()
    .catch(() => "");
  check(
    "UI: row shows Disabled after the toggle",
    /Disabled/.test(disabledBadge),
    JSON.stringify(disabledBadge.replace(/\s+/g, " ").slice(0, 120)),
  );
  await page.screenshot({ path: `${ARTIFACT_DIR}02-after-disable.png` });

  // ---- assert NAS + DB immediately -----------------------------------------
  const nas1 = await nasEnabled(WARM);
  const db1 = await dbRow();
  check(
    "NAS GET reflects disabled",
    !nas1.enabled?.includes(SLUG),
    `enabled=${JSON.stringify(nas1.enabled)}`,
  );
  check(
    "DB row reflects disabled",
    !db1?.enabledToolkits?.includes(SLUG),
    `enabledToolkits=${JSON.stringify(db1?.enabledToolkits)} toolOverrides=${JSON.stringify(db1?.toolOverrides)}`,
  );
  results.states.afterDisable = { nas: nas1.enabled, db: db1 };

  // ---- (6) reload: persisted, not optimistic -------------------------------
  await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "admin" }).click();
  await page.getByRole("textbox", { name: "Search toolkits" }).fill(NAME);
  const enableSwitchAfterReload = page.getByRole("switch", { name: `Enable ${NAME}`, exact: true });
  const reloadShowsDisabled = await enableSwitchAfterReload
    .waitFor({ state: "visible", timeout: 15000 })
    .then(() => true)
    .catch(() => false);
  check(
    "UI is not lying: full reload still shows disabled (persisted, not optimistic)",
    reloadShowsDisabled,
    reloadShowsDisabled
      ? `after reload the switch reads "Enable ${NAME}" (i.e. currently off)`
      : `after reload the switch did NOT read "Enable ${NAME}"`,
  );
  await page.screenshot({ path: `${ARTIFACT_DIR}03-after-reload-disabled.png` });

  // ---- (3) PROPAGATION to the gateway --------------------------------------
  // COLD principal: never touched the gateway, so no cache entry exists.
  const cold1 = await gwEnabled(COLD);
  results.timings.cold_disable_observed_ms_after_T0 = cold1.completedAt - T0;
  check(
    "GW cold principal sees the disable (no cache entry to invalidate)",
    cold1.status === 200 && cold1.toolkits !== null && !cold1.toolkits.includes(SLUG),
    `HTTP ${cold1.status} +${cold1.completedAt - T0}ms after T0, toolkits=${JSON.stringify(cold1.toolkits)}`,
  );
  // Guard against the fail-closed-to-empty false positive.
  check(
    "GW cold principal is a REAL org member (non-empty policy, not fail-closed)",
    (cold1.toolkits?.length ?? 0) > 0,
    `cold principal still sees ${cold1.toolkits?.length} other toolkit(s): ${JSON.stringify(cold1.toolkits)}`,
  );

  // WARM principal: must flip within the 30s TTL measured from the seeding fetch.
  const warmDeadline = warmFetchCompletedAt + 45_000;
  let warmFlipAt = null;
  let warmPolls = 0;
  let lastWarm = warm0;
  while (Date.now() < warmDeadline) {
    const w = await gwEnabled(WARM);
    warmPolls += 1;
    lastWarm = w;
    if (w.status === 200 && w.toolkits !== null && !w.toolkits.includes(SLUG)) {
      warmFlipAt = w.completedAt;
      break;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  const warmLagFromSeed = warmFlipAt ? warmFlipAt - warmFetchCompletedAt : null;
  const warmLagFromT0 = warmFlipAt ? warmFlipAt - T0 : null;
  results.timings.warm_disable_ms_after_cache_seed = warmLagFromSeed;
  results.timings.warm_disable_ms_after_T0 = warmLagFromT0;
  results.timings.warm_polls = warmPolls;
  check(
    "GW warm principal picks up the disable inside the 30s policy TTL",
    warmFlipAt !== null && warmLagFromSeed <= 32_000,
    warmFlipAt
      ? `flipped ${(warmLagFromSeed / 1000).toFixed(1)}s after the cache-seeding fetch (${(warmLagFromT0 / 1000).toFixed(1)}s after T0), ${warmPolls} polls`
      : `NEVER flipped within 45s; last=${JSON.stringify(lastWarm.toolkits)}`,
  );

  // ---- (4) RE-ENABLE through the UI ----------------------------------------
  const reSwitch = page.getByRole("switch", { name: `Enable ${NAME}`, exact: true });
  const putOnPromise = page.waitForResponse(
    (r) => r.request().method() === "PUT" && r.url().includes(`/toolkits/${SLUG}`),
    { timeout: 20000 },
  );
  // Seed the warm cache again right before re-enabling so the recovery clock
  // is measured the same way as the disable clock.
  const warmSeed2 = await gwEnabled(WARM);
  await reSwitch.click();
  const putOn = await putOnPromise;
  const T1 = Date.now();
  const putOnBody = await putOn.json().catch(() => null);
  check(
    "UI: re-enable click issued PUT enabled=true and got 200",
    putOn.status() === 200 && putOnBody?.enabled === true,
    `HTTP ${putOn.status()} body=${JSON.stringify(putOnBody)}`,
  );
  await page.getByRole("switch", { name: `Disable ${NAME}`, exact: true }).waitFor({ timeout: 10000 });
  await page.screenshot({ path: `${ARTIFACT_DIR}04-after-reenable.png` });

  const nas2 = await nasEnabled(WARM);
  const db2 = await dbRow();
  check(
    "NAS GET recovers after re-enable",
    nas2.enabled?.includes(SLUG),
    `enabled=${JSON.stringify(nas2.enabled)}`,
  );
  check(
    "DB row recovers after re-enable",
    db2?.enabledToolkits?.includes(SLUG),
    `enabledToolkits=${JSON.stringify(db2?.enabledToolkits)}`,
  );
  results.states.afterReenable = { nas: nas2.enabled, db: db2 };

  // reload again — persisted-enabled
  await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "admin" }).click();
  await page.getByRole("textbox", { name: "Search toolkits" }).fill(NAME);
  const reloadShowsEnabled = await page
    .getByRole("switch", { name: `Disable ${NAME}`, exact: true })
    .waitFor({ state: "visible", timeout: 15000 })
    .then(() => true)
    .catch(() => false);
  check(
    "UI is not lying: full reload after re-enable shows enabled",
    reloadShowsEnabled,
    `switch reads "Disable ${NAME}" after reload`,
  );
  await page.screenshot({ path: `${ARTIFACT_DIR}05-after-reload-enabled.png` });

  // gateway recovery, warm principal (cold principal is warm by now too)
  const recDeadline = warmSeed2.completedAt + 45_000;
  let recFlipAt = null;
  let recPolls = 0;
  let lastRec = null;
  while (Date.now() < recDeadline) {
    const w = await gwEnabled(WARM);
    recPolls += 1;
    lastRec = w;
    if (w.status === 200 && w.toolkits?.includes(SLUG)) {
      recFlipAt = w.completedAt;
      break;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  results.timings.warm_recovery_ms_after_cache_seed = recFlipAt
    ? recFlipAt - warmSeed2.completedAt
    : null;
  results.timings.warm_recovery_ms_after_T1 = recFlipAt ? recFlipAt - T1 : null;
  check(
    "GW recovers the toolkit after re-enable, inside the TTL",
    recFlipAt !== null && recFlipAt - warmSeed2.completedAt <= 32_000,
    recFlipAt
      ? `recovered ${((recFlipAt - warmSeed2.completedAt) / 1000).toFixed(1)}s after the cache-seeding fetch (${((recFlipAt - T1) / 1000).toFixed(1)}s after T1), ${recPolls} polls`
      : `NEVER recovered within 45s; last=${JSON.stringify(lastRec?.toolkits)}`,
  );

  check(
    "no console errors during the round-trip",
    consoleErrors.length === 0,
    consoleErrors.length ? JSON.stringify(consoleErrors.slice(0, 3)) : "none",
  );

  results.putCalls = putCalls;
  await browser.close();

  // ---- (5) restore check (the actual restore is done by run.sh) ------------
  const dbFinal = await dbRow();
  results.states.final = dbFinal;
  const sameSet =
    JSON.stringify([...(db0?.enabledToolkits ?? [])].sort()) ===
    JSON.stringify([...(dbFinal?.enabledToolkits ?? [])].sort());
  const sameOrder =
    JSON.stringify(db0?.enabledToolkits) === JSON.stringify(dbFinal?.enabledToolkits);
  check("restore: enabled toolkit SET matches the baseline", sameSet, `${JSON.stringify(dbFinal?.enabledToolkits)}`);
  if (sameSet && !sameOrder) {
    finding(
      "low",
      "A UI disable+re-enter round-trip silently REORDERS OrgToolkitPolicy.enabledToolkits",
      `baseline=${JSON.stringify(db0?.enabledToolkits)} after=${JSON.stringify(dbFinal?.enabledToolkits)} ` +
        `— setToolkitEnabled() rebuilds the array as [...existing, slug], so the re-enabled slug moves to the tail. ` +
        `The set is preserved; only the stored order changes.`,
    );
  }
  check(
    "restore: toolOverrides matches the baseline",
    JSON.stringify(db0?.toolOverrides ?? null) === JSON.stringify(dbFinal?.toolOverrides ?? null),
    `baseline=${JSON.stringify(db0?.toolOverrides ?? null)} final=${JSON.stringify(dbFinal?.toolOverrides ?? null)}`,
  );

  writeFileSync(`${ARTIFACT_DIR}roundtrip_results.json`, JSON.stringify(results, null, 2));
  const failed = results.checks.filter((c) => !c.pass);
  console.log(
    `\n== ${results.checks.length - failed.length}/${results.checks.length} checks passed, ${results.findings.length} finding(s) ==`,
  );
  console.log("timings:", JSON.stringify(results.timings, null, 2));
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => {
  console.error("FATAL", e);
  if (existsSync(ARTIFACT_DIR)) {
    writeFileSync(`${ARTIFACT_DIR}roundtrip_results.json`, JSON.stringify(results, null, 2));
  }
  process.exit(2);
});
