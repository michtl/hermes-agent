import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// App.tsx pulls in a large dependency graph (plugin discovery, websocket
// status, persistent chat host, etc.) that makes a full render expensive to
// stand up in isolation. The /mcp -> /capabilities redirect is a single,
// static route wiring, so it's verified at the source level instead: this
// asserts both that a dedicated `/mcp` route exists and that it resolves to
// `/capabilities` via <Navigate>, rather than falling through to
// UnknownRouteFallback's default of /sessions.
const appSource = readFileSync(
  fileURLToPath(new URL("./App.tsx", import.meta.url)),
  "utf-8",
);

describe("/mcp route", () => {
  it("is registered as an explicit route (not left to the catch-all)", () => {
    expect(appSource).toMatch(/<Route\s+path="\/mcp"\s+element=\{<McpRedirect/);
  });

  it("redirects to /capabilities, not the catch-all's /sessions default", () => {
    const fn = appSource.split("function McpRedirect")[1]?.split("\n\n")[0];
    expect(fn).toBeTruthy();
    expect(fn).toContain('<Navigate to="/capabilities" replace />');
  });
});
