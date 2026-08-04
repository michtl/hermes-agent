// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PageHeaderContext } from "@/contexts/page-header-context";
import { api, type CapabilityToolkit } from "@/lib/api";
import CapabilitiesPage, { parseToolScope } from "./CapabilitiesPage";

vi.mock("@/components/McpServersSection", () => ({
  McpServersSection: () => null,
}));

vi.mock("@nous-research/ui/hooks/use-toast", () => ({
  useToast: () => ({ toast: null, showToast: vi.fn() }),
}));

vi.mock("@nous-research/ui/ui/components/toast", () => ({
  Toast: () => null,
}));

const github: CapabilityToolkit = {
  slug: "github",
  name: "GitHub",
  enabled: true,
  connected: true,
};

let container: HTMLDivElement;
let root: Root;

async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function renderPage(toolkits: CapabilityToolkit[]): Promise<void> {
  vi.spyOn(api, "getCapabilityToolkits").mockResolvedValueOnce({ toolkits });
  await act(async () => {
    root.render(
      <PageHeaderContext.Provider
        value={{ setAfterTitle: vi.fn(), setEnd: vi.fn(), setTitle: vi.fn() }}
      >
        <CapabilitiesPage />
      </PageHeaderContext.Provider>,
    );
  });
  await settle();
}

function clickText(text: string): void {
  const element = [...container.querySelectorAll<HTMLElement>("*")].find(
    (candidate) => candidate.textContent?.trim() === text,
  );
  expect(element, `element with text ${text}`).toBeTruthy();
  act(() => element!.dispatchEvent(new MouseEvent("click", { bubbles: true })));
}

function scopeTextarea(): HTMLTextAreaElement {
  const textarea = container.querySelector<HTMLTextAreaElement>(
    'textarea[aria-label="Tool slugs"]',
  );
  expect(textarea).toBeTruthy();
  return textarea!;
}

function changeScope(value: string): void {
  const textarea = scopeTextarea();
  act(() => {
    const setter = Object.getOwnPropertyDescriptor(
      HTMLTextAreaElement.prototype,
      "value",
    )?.set;
    setter?.call(textarea, value);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("CapabilitiesPage tool scope", () => {
  it("renders all three states and resets the textarea when selection changes", async () => {
    await renderPage([
      github,
      {
        ...github,
        slug: "slack",
        name: "Slack",
        toolsOverride: ["SLACK_SEND_MESSAGE", "SLACK_LIST_CHANNELS"],
      },
      {
        ...github,
        slug: "notion",
        name: "Notion",
        toolsOverride: [],
      },
    ]);

    clickText("GitHub");
    expect(container.textContent).toContain("All tools allowed");
    expect(scopeTextarea().value).toBe("");
    changeScope("UNSAVED_TOOL");

    clickText("Slack");
    expect(container.textContent).toContain("Scoped to 2 tools");
    expect(scopeTextarea().value).toBe(
      "SLACK_SEND_MESSAGE\nSLACK_LIST_CHANNELS",
    );

    clickText("Notion");
    expect(container.textContent).toContain(
      "Scoped to none — all tools blocked",
    );
    expect(container.textContent).toContain(
      "This explicit empty scope blocks every tool in the toolkit.",
    );
    expect(scopeTextarea().value).toBe("");
  });

  it("parses, trims, and de-duplicates a populated scope before saving", async () => {
    await renderPage([github]);
    clickText("GitHub");
    changeScope(" GITHUB_GET_REPOS, GITHUB_GET_ISSUES\nGITHUB_GET_REPOS ");
    vi.spyOn(api, "setToolkitScope").mockResolvedValue({
      slug: "github",
      enabled: true,
      enabledToolkits: ["github"],
    });
    vi.mocked(api.getCapabilityToolkits).mockResolvedValueOnce({
      toolkits: [
        {
          ...github,
          toolsOverride: ["GITHUB_GET_REPOS", "GITHUB_GET_ISSUES"],
        },
      ],
    });

    clickText("Save scope");
    await settle();

    expect(api.setToolkitScope).toHaveBeenCalledWith("github", {
      enabled: true,
      tools: ["GITHUB_GET_REPOS", "GITHUB_GET_ISSUES"],
    });
    expect(container.textContent).toContain("Scoped to 2 tools");
  });

  it("confirms and forwards an explicit empty deny-all scope", async () => {
    await renderPage([github]);
    clickText("GitHub");
    const confirm = vi.fn(() => true);
    vi.stubGlobal("confirm", confirm);
    vi.spyOn(api, "setToolkitScope").mockResolvedValue({
      slug: "github",
      enabled: true,
      enabledToolkits: ["github"],
      toolsOverride: [],
    });
    vi.mocked(api.getCapabilityToolkits).mockResolvedValueOnce({
      toolkits: [{ ...github, toolsOverride: [] }],
    });

    clickText("Save scope");
    await settle();

    expect(confirm).toHaveBeenCalledWith(
      "Save an empty scope? This blocks all tools in this toolkit.",
    );
    expect(api.setToolkitScope).toHaveBeenCalledWith("github", {
      enabled: true,
      tools: [],
    });
    expect(container.textContent).toContain(
      "Scoped to none — all tools blocked",
    );
  });

  it("shows the blocked warning and enables clearing a deny-all override", async () => {
    await renderPage([{ ...github, toolsOverride: [] }]);
    clickText("GitHub");

    expect(container.textContent).toContain(
      "Scoped to none — all tools blocked",
    );
    expect(container.textContent).toContain(
      "This explicit empty scope blocks every tool in the toolkit.",
    );
    const clearOverride = [...container.querySelectorAll("button")].find(
      (button) => button.textContent?.trim() === "Clear override",
    );
    expect(clearOverride).toBeTruthy();
    expect(clearOverride!.disabled).toBe(false);
  });

  // NOTE: this assertion was flipped from "omits the tools key" to "sends an
  // explicit null". NAS's updated contract makes an ABSENT `tools` field mean
  // "preserve the existing scope" (so the enable/disable toggle no longer
  // destroys it) and reserves `tools: null` as the explicit clear-override
  // signal. Omitting the field here would now be a no-op, not a clear.
  it("clears an override by sending an explicit tools: null", async () => {
    await renderPage([{ ...github, toolsOverride: ["GITHUB_GET_REPOS"] }]);
    clickText("GitHub");
    vi.spyOn(api, "setToolkitScope").mockResolvedValue({
      slug: "github",
      enabled: true,
      enabledToolkits: ["github"],
    });
    vi.mocked(api.getCapabilityToolkits).mockResolvedValueOnce({
      toolkits: [github],
    });

    clickText("Clear override");
    await settle();

    expect(api.setToolkitScope).toHaveBeenCalledWith("github", {
      enabled: true,
      tools: null,
    });
    const update = vi.mocked(api.setToolkitScope).mock.calls[0][1];
    expect(update).toHaveProperty("tools", null);
    expect(container.textContent).toContain("All tools allowed");
  });

  it("toggling enabled/disabled sends no tools field, preserving scope", async () => {
    await renderPage([{ ...github, toolsOverride: ["GITHUB_GET_REPOS"] }]);
    vi.spyOn(api, "setToolkitEnabled").mockResolvedValue({
      slug: "github",
      enabled: false,
      enabledToolkits: [],
      toolsOverride: ["GITHUB_GET_REPOS"],
    });

    const toggle = container.querySelector(
      'button[aria-label="Disable GitHub"]',
    ) as HTMLButtonElement | null;
    expect(toggle).toBeTruthy();
    toggle!.click();
    await settle();

    expect(api.setToolkitEnabled).toHaveBeenCalledWith("github", false);
  });
});

describe("parseToolScope", () => {
  it("accepts commas and newlines while dropping blanks and duplicates", () => {
    expect(parseToolScope(" A, B\nA, , C ")).toEqual(["A", "B", "C"]);
  });
});
