import { describe, expect, it } from "vitest";

import type { CapabilityToolkit } from "./api";
import {
  FALLBACK_TOOLKIT_BRAND,
  mergeCapabilityToolkit,
  scrubVendorMentions,
} from "./capability-catalog";

describe("mergeCapabilityToolkit", () => {
  it("uses local presentation data while preferring the server description", () => {
    const serverDescription =
      "Send, receive, and organize email using your Gmail account.";
    const toolkit: CapabilityToolkit = {
      slug: "gmail",
      name: "Gmail",
      enabled: true,
      connected: true,
      description: serverDescription,
      category: "email",
    };

    const result = mergeCapabilityToolkit(toolkit);

    expect(result.brand).toBe("#EA4335");
    expect(result.category).toBe("Communication");
    expect(result.description).toBe(serverDescription);
    expect(result.description).not.toBe("Send & manage email.");
  });

  it("uses server metadata and the fallback brand for an unmatched slug", () => {
    const toolkit: CapabilityToolkit = {
      slug: "posthog",
      name: "PostHog",
      enabled: false,
      connected: false,
      category: "Analytics",
      description: "Product analytics.",
    };

    const result = mergeCapabilityToolkit(toolkit);

    expect(result.category).toBe("Analytics");
    expect(result.description).toBe("Product analytics.");
    expect(result.brand).toBe(FALLBACK_TOOLKIT_BRAND);
  });

  it("uses empty metadata fallbacks for an unmatched slug", () => {
    const toolkit: CapabilityToolkit = {
      slug: "posthog",
      name: "PostHog",
      enabled: false,
      connected: false,
    };

    const result = mergeCapabilityToolkit(toolkit);

    expect(result.category).toBe("Other");
    expect(result.description).toBe("");
    expect(result.brand).toBe(FALLBACK_TOOLKIT_BRAND);
  });

  it("passes through an explicit empty tool override unchanged", () => {
    const toolkit: CapabilityToolkit = {
      slug: "github",
      name: "GitHub",
      enabled: true,
      connected: true,
      toolsOverride: [],
    };

    const result = mergeCapabilityToolkit(toolkit);

    expect(result.toolsOverride).toBe(toolkit.toolsOverride);
    expect(result.toolsOverride).toEqual([]);
  });

  it("scrubs a vendor mention in the server description so it never reaches the DOM", () => {
    const toolkit: CapabilityToolkit = {
      slug: "browser_tool",
      name: "Browser Tool",
      enabled: true,
      connected: true,
      description:
        "Composio's browser automation tool for navigating and scraping pages.",
    };

    const result = mergeCapabilityToolkit(toolkit);

    expect(result.description.toLowerCase()).not.toContain("composio");
    // Redacted, not blanked: the sentence still reads.
    expect(result.description).toBe(
      "the tool provider's browser automation tool for navigating and scraping pages.",
    );
  });

  it("scrubs mid-sentence and mixed-case vendor mentions", () => {
    expect(scrubVendorMentions("Built by COMPOSIO for agents.")).toBe(
      "Built by the tool provider for agents.",
    );
    expect(scrubVendorMentions("No vendor mention here.")).toBe(
      "No vendor mention here.",
    );
  });
});

describe("catalog rendering guard", () => {
  it("scans a rendered catalog fixture and finds no vendor substring", () => {
    const rawToolkits: CapabilityToolkit[] = [
      {
        slug: "browser_tool",
        name: "Browser Tool",
        enabled: true,
        connected: true,
        description: "Composio's browser automation tool.",
      },
      {
        slug: "gmail",
        name: "Gmail",
        enabled: true,
        connected: true,
        description: "Send & manage email.",
      },
    ];

    const rendered = rawToolkits
      .map(mergeCapabilityToolkit)
      .map((t) => `${t.name}|${t.category}|${t.description}`)
      .join("\n");

    expect(rendered.toLowerCase()).not.toContain("composio");
  });
});
