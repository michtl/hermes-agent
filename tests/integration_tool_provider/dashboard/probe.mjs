#!/usr/bin/env node
/**
 * Dashboard lens probe (Playwright, read-only against a live dashboard).
 *
 * Covers: (a) live top-100 catalog render + enabled-toolkit distinction,
 * (b) no-vendor-string scan (DOM + network response bodies + console),
 * (c) search/category filters incl. empty state / clear / no-match / combo,
 * (d) console/network-error hygiene, (e) MCP section add/test/remove +
 * config.yaml persistence + /mcp redirect, (f) load/fetch latency.
 *
 * Run:
 *   BASE_URL=http://127.0.0.1:8091 HERMES_HOME=/tmp/.../hermes-home \
 *     NAS_URL=http://127.0.0.1:3111 NAS_TOKEN_FILE=/tmp/.../.token \
 *     node probe.mjs
 *
 * Requires a `node_modules` symlink to a Playwright install with chromium
 * already downloaded (see README.md in this directory).
 */
import { chromium } from "playwright";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const BASE_URL = process.env.BASE_URL ?? "http://127.0.0.1:8091";
const HERMES_HOME = process.env.HERMES_HOME ?? "";
const NAS_URL = process.env.NAS_URL ?? "http://127.0.0.1:3111";
const NAS_TOKEN_FILE = process.env.NAS_TOKEN_FILE ?? "";
const VENDOR = "composio"; // scan pattern only — never printed in prose findings.
const ARTIFACT_DIR = fileURLToPath(new URL(".", import.meta.url));

const results = { checks: [], findings: [], timings: {} };
function check(name, pass, evidence) {
  results.checks.push({ name, pass, evidence });
  console.log(`[${pass ? "PASS" : "FAIL"}] ${name} :: ${evidence}`);
}
function finding(severity, summary, evidence) {
  results.findings.push({ severity, summary, evidence });
  console.log(`[FINDING:${severity}] ${summary} :: ${evidence}`);
}

function configYamlPath() {
  return path.join(HERMES_HOME, "config.yaml");
}
function readConfigYaml() {
  const p = configYamlPath();
  return existsSync(p) ? readFileSync(p, "utf8") : "";
}

async function main() {
  // ---- live NAS catalog (ground truth for comparison) ----
  let nasToolkits = [];
  if (NAS_TOKEN_FILE) {
    const token = readFileSync(NAS_TOKEN_FILE, "utf8").trim();
    const resp = await fetch(`${NAS_URL}/api/portal/tools/toolkits`, {
      headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
    });
    const body = await resp.json();
    nasToolkits = Array.isArray(body?.toolkits) ? body.toolkits : [];
  }
  const nasEnabled = new Set(
    nasToolkits.filter((t) => t.enabled).map((t) => t.slug),
  );
  console.log(
    `NAS ground truth: ${nasToolkits.length} toolkits, ${nasEnabled.size} enabled: [${[...nasEnabled].join(", ")}]`,
  );

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1400, height: 1000 } });
  const page = await context.newPage();

  // ---- collectors: console, page errors, network ----
  const consoleMessages = [];
  const pageErrors = [];
  const failedRequests = [];
  const responseBodies = []; // { url, status, contentType, body }

  page.on("console", (msg) => {
    consoleMessages.push({ type: msg.type(), text: msg.text() });
  });
  page.on("pageerror", (err) => {
    pageErrors.push(String(err));
  });
  page.on("requestfailed", (req) => {
    failedRequests.push({ url: req.url(), failure: req.failure()?.errorText });
  });
  page.on("response", async (resp) => {
    try {
      const ct = resp.headers()["content-type"] ?? "";
      if (ct.includes("json") || ct.includes("text")) {
        const body = await resp.text();
        responseBodies.push({ url: resp.url(), status: resp.status(), contentType: ct, body });
      } else {
        responseBodies.push({ url: resp.url(), status: resp.status(), contentType: ct, body: null });
      }
    } catch {
      // response body unavailable (e.g. redirect/opaque) — not fatal.
    }
  });

  // ================= (a)+(f) RENDER + LATENCY =================
  const navStart = Date.now();
  await page.goto(`${BASE_URL}/capabilities`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('h2:has-text("App toolkits")', { timeout: 15000 });
  // Wait for the toolkit fetch to settle by waiting for a card or the empty state.
  await page.waitForFunction(
    () => document.querySelectorAll('[aria-label="Toolkit categories"]').length > 0,
    { timeout: 15000 },
  );
  await page.waitForTimeout(500); // let the toolkit grid paint after fetch resolves
  const loadMs = Date.now() - navStart;
  results.timings.page_load_wallclock_ms = loadMs; // includes this script's settle waits
  const navTiming = await page.evaluate(() => {
    const nav = performance.getEntriesByType("navigation")[0];
    return nav ? { domContentLoadedMs: nav.domContentLoadedEventEnd, loadEventMs: nav.loadEventEnd } : null;
  });
  results.timings.navigation_timing = navTiming;

  const catalogResp = responseBodies.find((r) => r.url.endsWith("/api/capabilities/toolkits"));
  results.timings.catalog_fetch_seen = Boolean(catalogResp);
  const catalogResourceTiming = await page.evaluate(() => {
    const entry = performance
      .getEntriesByType("resource")
      .find((e) => e.name.includes("/api/capabilities/toolkits"));
    return entry ? { durationMs: entry.duration, startMs: entry.startTime } : null;
  });
  results.timings.catalog_fetch_duration_ms = catalogResourceTiming?.durationMs ?? null;

  const headingText = await page.locator('h2:has-text("App toolkits")').first().innerText();
  const renderedCountMatch = headingText.match(/\((\d+)\)/);
  const renderedCount = renderedCountMatch ? Number(renderedCountMatch[1]) : -1;
  check(
    "catalog count matches NAS",
    renderedCount === nasToolkits.length,
    `rendered=${renderedCount} nas=${nasToolkits.length}`,
  );

  // Enabled/disabled distinguishability: for each NAS-enabled slug, the card
  // must NOT show the "Disabled" badge; for a NAS-disabled sample, it must.
  // The section wraps TWO nested grids: an outer 2-col layout grid (content +
  // sidebar) and CatalogGrid's own card grid inside it. Disambiguate by the
  // card grid's distinctive Tailwind classes (sm:grid-cols-2 lg:grid-cols-3),
  // which the outer layout grid does not carry — else `.grid` matches the
  // wrong (2-child) container and every downstream count/text check is noise.
  const CARD_GRID_FINDER = `
    (() => {
      const grid = [...document.querySelectorAll("div")].find(
        (d) => d.className.includes("sm:grid-cols-2") && d.className.includes("gap-3"),
      );
      return grid ?? null;
    })()
  `;
  const catalogCards = await page.evaluate(`
    (() => {
      const grid = ${CARD_GRID_FINDER};
      if (!grid) return [];
      const children = [...grid.children];
      if (children.length === 1 && children[0].textContent?.includes("No toolkits match")) return [];
      return children.map((card) => card.textContent || "");
    })()
  `);
  check(
    "catalog grid rendered cards",
    catalogCards.length === renderedCount || catalogCards.length > 0,
    `grid children=${catalogCards.length} headingCount=${renderedCount}`,
  );

  let enabledMismatch = [];
  for (const slug of nasEnabled) {
    const entry = nasToolkits.find((t) => t.slug === slug);
    const name = entry?.name ?? slug;
    const card = catalogCards.find((t) => t.includes(name));
    if (!card) {
      enabledMismatch.push(`${slug}: no card found`);
      continue;
    }
    if (card.includes("Disabled")) {
      enabledMismatch.push(`${slug}: rendered as Disabled`);
    }
  }
  check(
    "6 org-enabled toolkits render as enabled (not Disabled badge)",
    enabledMismatch.length === 0,
    enabledMismatch.length === 0 ? "all 6 distinguishable" : enabledMismatch.join("; "),
  );

  const disabledSample = nasToolkits.find((t) => !t.enabled);
  if (disabledSample) {
    const card = catalogCards.find((t) => t.includes(disabledSample.name));
    check(
      "a NAS-disabled toolkit renders Disabled badge",
      Boolean(card && card.includes("Disabled")),
      `sample=${disabledSample.slug} cardFound=${Boolean(card)} hasDisabledBadge=${Boolean(card?.includes("Disabled"))}`,
    );
  }

  // No toolkit literally named/slugged VENDOR appears.
  const vendorNamedCard = catalogCards.some((t) => t.toLowerCase().includes(VENDOR));
  check(
    `no toolkit literally named "${VENDOR}" in catalog`,
    !vendorNamedCard,
    vendorNamedCard ? "FOUND a card mentioning the vendor" : "absent (correct)",
  );
  const vendorInNas = nasToolkits.some((t) => (t.slug || "").toLowerCase().includes(VENDOR));
  check(
    `NAS catalog itself has no "${VENDOR}"-slugged toolkit`,
    !vendorInNas,
    `nasHasVendorSlug=${vendorInNas}`,
  );

  // ================= (b) NO VENDOR ANYWHERE =================
  const domHtml = await page.content();
  const domHits = [];
  const domLower = domHtml.toLowerCase();
  let idx = domLower.indexOf(VENDOR);
  while (idx !== -1) {
    domHits.push(domHtml.slice(Math.max(0, idx - 60), idx + 60));
    idx = domLower.indexOf(VENDOR, idx + 1);
  }
  check(`DOM contains no "${VENDOR}"`, domHits.length === 0, `hits=${domHits.length}`);
  if (domHits.length) {
    finding("medium", `Vendor literal present in rendered DOM (${domHits.length}x)`, domHits.slice(0, 5).join(" ||| "));
  }

  const CONNECT_URL_HOST_EXEMPT = /connect|oauth|authorize/i;
  const bodyHits = [];
  for (const r of responseBodies) {
    if (!r.body) continue;
    const lower = r.body.toLowerCase();
    let bidx = lower.indexOf(VENDOR);
    while (bidx !== -1) {
      const snippet = r.body.slice(Math.max(0, bidx - 60), bidx + 80);
      bodyHits.push({ url: r.url, snippet });
      bidx = lower.indexOf(VENDOR, bidx + 1);
    }
  }
  // Exempt only literal OAuth/connect URL fields, not bulk catalog fields like image/logo URLs.
  const nonExemptBodyHits = bodyHits.filter((h) => !/connect_url"\s*:/i.test(h.snippet) );
  check(
    `no network RESPONSE body contains "${VENDOR}" outside a connect_url field`,
    nonExemptBodyHits.length === 0,
    `totalHits=${bodyHits.length} nonExempt=${nonExemptBodyHits.length}`,
  );
  if (nonExemptBodyHits.length) {
    const byUrl = new Map();
    for (const h of nonExemptBodyHits) {
      if (!byUrl.has(h.url)) byUrl.set(h.url, []);
      byUrl.get(h.url).push(h.snippet);
    }
    for (const [url, snippets] of byUrl) {
      finding(
        "medium",
        `Vendor literal present in network response body from ${url}`,
        `count=${snippets.length} sample="${snippets[0]}"`,
      );
    }
  }

  const consoleHits = consoleMessages.filter((m) => m.text.toLowerCase().includes(VENDOR));
  check(`no console log line contains "${VENDOR}"`, consoleHits.length === 0, `hits=${consoleHits.length}`);
  if (consoleHits.length) {
    finding("low", "Vendor literal present in console log", JSON.stringify(consoleHits.slice(0, 3)));
  }

  // ================= (c) FILTERS =================
  const searchBox = page.getByPlaceholder("Search toolkits");

  async function currentCatalogCards() {
    return page.evaluate(`
      (() => {
        const grid = ${CARD_GRID_FINDER};
        if (!grid) return [];
        const children = [...grid.children];
        if (children.length === 1 && children[0].textContent?.includes("No toolkits match")) return [];
        return children.map((card) => card.textContent || "");
      })()
    `);
  }
  async function currentCatalogCount() {
    return (await currentCatalogCards()).length;
  }

  await searchBox.fill("Slack");
  await page.waitForTimeout(150);
  const slackCards = await currentCatalogCards();
  check(
    'search "Slack" subsets to matching toolkit(s) only',
    slackCards.length > 0 && slackCards.every((t) => t.toLowerCase().includes("slack")),
    `matches=${slackCards.length} allContainSlack=${slackCards.every((t) => t.toLowerCase().includes("slack"))}`,
  );

  await searchBox.fill("zzz-no-such-toolkit-zzz");
  await page.waitForTimeout(150);
  const noMatchText = await page.locator("text=No toolkits match.").count();
  check("no-match query shows empty state, no crash", noMatchText > 0, `emptyStateVisible=${noMatchText > 0}`);
  const noMatchErrorsSoFar = pageErrors.length;

  await searchBox.fill("");
  await page.waitForTimeout(150);
  const clearedCount = await currentCatalogCount();
  check("clearing search restores full catalog count", clearedCount === nasToolkits.length, `count=${clearedCount}`);

  // Category filter: pick a non-"All" category button and confirm subsetting.
  const categoryButtons = await page.locator('[aria-label="Toolkit categories"] button').allInnerTexts();
  const nonAllCategory = categoryButtons.find((c) => c !== "All");
  if (nonAllCategory) {
    await page.locator('[aria-label="Toolkit categories"] button', { hasText: nonAllCategory }).first().click();
    await page.waitForTimeout(150);
    const catCount = await currentCatalogCount();
    check(
      `category filter "${nonAllCategory}" subsets catalog`,
      catCount > 0 && catCount < nasToolkits.length,
      `count=${catCount} totalNas=${nasToolkits.length}`,
    );

    // Combined filter + search.
    await searchBox.fill("a");
    await page.waitForTimeout(150);
    const comboCount = await currentCatalogCount();
    check(
      "category + search combination narrows further (or holds, never widens)",
      comboCount <= catCount,
      `comboCount=${comboCount} categoryOnlyCount=${catCount}`,
    );

    // Restore to All / cleared for the rest of the run.
    await searchBox.fill("");
    await page.locator('[aria-label="Toolkit categories"] button', { hasText: "All" }).first().click();
    await page.waitForTimeout(150);
  } else {
    check("category filter present", false, "no non-All category button found");
  }

  // ================= (d) console / network hygiene =================
  const reactWarnings = consoleMessages.filter(
    (m) => m.type === "warning" && /key prop|unique "key" prop|Warning: /i.test(m.text),
  );
  const hardErrors = consoleMessages.filter((m) => m.type === "error");
  check("zero uncaught page errors", pageErrors.length === 0, `count=${pageErrors.length}`);
  check("zero console.error messages", hardErrors.length === 0, `count=${hardErrors.length} sample=${JSON.stringify(hardErrors.slice(0, 3))}`);
  check("zero failed network requests", failedRequests.length === 0, `count=${failedRequests.length} sample=${JSON.stringify(failedRequests.slice(0, 3))}`);
  if (reactWarnings.length) {
    finding("low", "React key/prop warnings present in console", JSON.stringify(reactWarnings.slice(0, 5)));
  }
  const http4xx5xx = responseBodies.filter((r) => r.status >= 400);
  check("zero 4xx/5xx API responses during render+filter flow", http4xx5xx.length === 0, `count=${http4xx5xx.length} sample=${JSON.stringify(http4xx5xx.slice(0,3).map(r=>({url:r.url,status:r.status})))}`);

  // ================= (e) MCP SECTION =================
  const mcpServerName = `probe-stub-${Date.now()}`;
  await page.locator("text=Your MCP servers").scrollIntoViewIfNeeded();
  const mcpFetchResp = responseBodies.find((r) => r.url.endsWith("/api/mcp/servers") && r.status === 200);
  results.timings.mcp_servers_fetch_seen = Boolean(mcpFetchResp);

  // Scope "is this name present" checks to the MCP server LIST container,
  // not the whole page — a delete/add success toast also echoes the server
  // name and would otherwise produce a false "still visible" reading.
  async function mcpListContainsName(name) {
    return page.evaluate((needle) => {
      const heading = [...document.querySelectorAll("h2")].find((h) =>
        h.textContent?.includes("Your MCP servers"),
      );
      const container = heading?.parentElement?.parentElement;
      return Boolean(container?.textContent?.includes(needle));
    }, name);
  }
  async function mcpRowText(name) {
    return page.evaluate((needle) => {
      const heading = [...document.querySelectorAll("h2")].find((h) =>
        h.textContent?.includes("Your MCP servers"),
      );
      const container = heading?.parentElement?.parentElement;
      if (!container) return "";
      const nameEl = [...container.querySelectorAll("span")].find((s) =>
        s.textContent?.includes(needle),
      );
      return nameEl?.closest('[class*="py-4"]')?.textContent ?? "";
    }, name);
  }

  await page.getByRole("button", { name: "Add Server" }).click();
  await page.getByLabel("Name").fill(mcpServerName);
  // Switch transport to stdio (custom combobox, not a native <select>).
  await page.locator("#mcp-transport [role=combobox]").click();
  await page.getByRole("option", { name: "stdio" }).click();
  await page.getByLabel("Command").fill("/bin/true");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await page.waitForTimeout(800);

  const configAfterAdd = readConfigYaml();
  const persistedAfterAdd = configAfterAdd.includes(mcpServerName);
  check(
    "added MCP server persists to config.yaml",
    persistedAfterAdd,
    `name=${mcpServerName} foundInConfigYaml=${persistedAfterAdd}`,
  );

  const rowPresent = await mcpListContainsName(mcpServerName);
  check("added MCP server visible in Your MCP servers list", rowPresent, `present=${rowPresent}`);

  if (rowPresent) {
    const serverRow = page.locator(`text=${mcpServerName}`).first();
    const cardContainer = serverRow.locator("xpath=ancestor::*[contains(@class,'py-4')][1]");
    const testBtn = cardContainer.getByRole("button", { name: "Test connection" });
    await testBtn.click();
    await page.waitForTimeout(1000);
    const testResultText = await mcpRowText(mcpServerName);
    check(
      "Test connection exercises without crashing the page",
      pageErrors.length === noMatchErrorsSoFar, // no NEW page errors from this action
      `rowTextAfterTest="${testResultText.replace(/\s+/g, " ").trim()}"`,
    );

    // Remove it — scope the trigger to this server's row, not any other Delete button.
    await cardContainer.getByRole("button", { name: "Delete" }).click();
    await page.waitForTimeout(300);
    const dialog = page.getByRole("alertdialog");
    const dialogVisible = await dialog.isVisible().catch(() => false);
    if (dialogVisible) {
      await dialog.getByRole("button", { name: "Delete" }).click();
    } else {
      // Fallback: no alertdialog role found — try last "Delete"-named button.
      await page.getByRole("button", { name: "Delete" }).last().click();
    }
    await page.waitForTimeout(800);

    const rowGonePresent = await mcpListContainsName(mcpServerName);
    check("MCP server removed from UI list", !rowGonePresent, `stillPresentInListContainer=${rowGonePresent}`);

    const configAfterRemove = readConfigYaml();
    const stillInConfig = configAfterRemove.includes(mcpServerName);
    check("MCP server removed from config.yaml", !stillInConfig, `stillInConfigYaml=${stillInConfig}`);
  }

  // /mcp -> /capabilities redirect assertion.
  await page.goto(`${BASE_URL}/mcp`, { waitUntil: "networkidle" });
  await page.waitForTimeout(500);
  const finalUrl = page.url();
  const finalPath = new URL(finalUrl).pathname;
  check(
    "/mcp redirects to /capabilities",
    finalPath === "/capabilities",
    `finalPath=${finalPath} (finalUrl=${finalUrl})`,
  );
  if (finalPath !== "/capabilities") {
    finding(
      "high",
      `Navigating to /mcp does NOT redirect to /capabilities — it lands on ${finalPath} instead`,
      `No client route named "/mcp" exists in App.tsx; the unmatched-route fallback (UnknownRouteFallback) sends every unknown path, including /mcp, to /sessions. There is no code anywhere in web/src implementing an /mcp -> /capabilities redirect.`,
    );
  }

  await browser.close();

  const summary = {
    timings: results.timings,
    checks: results.checks,
    findings: results.findings,
    nas_toolkit_count: nasToolkits.length,
    nas_enabled: [...nasEnabled],
  };
  console.log("\n=== SUMMARY ===");
  console.log(JSON.stringify(summary, null, 2));
}

main().catch((err) => {
  console.error("PROBE CRASHED:", err);
  process.exit(1);
});
