import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
} from "react";
import { Blocks, ExternalLink, RefreshCw, Search, Wrench, X } from "lucide-react";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Switch } from "@nous-research/ui/ui/components/switch";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { McpServersSection } from "@/components/McpServersSection";
import { usePageHeader } from "@/contexts/usePageHeader";
import { useModalBehavior } from "@/hooks/useModalBehavior";
import { api } from "@/lib/api";
import type { CapabilityToolkit } from "@/lib/api";
import {
  CAPABILITY_CATEGORIES,
  RECOMMENDED_TOOLS,
  mergeCapabilityToolkit,
} from "@/lib/capability-catalog";
import type { MergedCapabilityToolkit } from "@/lib/capability-catalog";
import { cn, themedBody } from "@/lib/utils";

type CapabilityView = "catalog" | "admin";
type AdminStubStatus = "soft" | "revoked";
type ToolkitAction = "enabled" | "disabled" | "soft-disabled" | "revoked";

interface ActionLogEntry {
  id: string;
  time: string;
  action: ToolkitAction;
  toolkitName: string;
}

const STARTER_TOOLKIT_SLUGS = [
  "gmail",
  "slack",
  "github",
  "linear",
  "notion",
  "googlecalendar",
];

function toolkitInitials(name: string): string {
  return name
    .replace(/\(.*\)/, "")
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((word) => word[0])
    .join("")
    .toUpperCase();
}

function BrandGlyph({
  toolkit,
  size = "md",
}: {
  toolkit: MergedCapabilityToolkit;
  size?: "md" | "lg";
}) {
  return (
    <span
      className={cn(
        "flex shrink-0 items-center justify-center rounded text-sm font-bold text-white",
        size === "lg" ? "h-12 w-12 text-base" : "h-10 w-10",
      )}
      style={{ background: toolkit.brand }}
      aria-hidden="true"
    >
      {toolkitInitials(toolkit.name)}
    </span>
  );
}

function ToolkitStatus({ toolkit }: { toolkit: MergedCapabilityToolkit }) {
  if (toolkit.connected) {
    return <Badge tone="success">Connected</Badge>;
  }
  if (toolkit.enabled) {
    return <Badge tone="secondary">Not connected</Badge>;
  }
  return <Badge tone="outline">Disabled</Badge>;
}

export function parseToolScope(value: string): string[] {
  return [
    ...new Set(
      value
        .split(/[\n,]/)
        .map((slug) => slug.trim())
        .filter(Boolean),
    ),
  ];
}

function toolScopeDraft(toolkit: CapabilityToolkit | undefined): string {
  return toolkit?.toolsOverride?.join("\n") ?? "";
}

export default function CapabilitiesPage() {
  const [toolkits, setToolkits] = useState<CapabilityToolkit[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("All");
  const [view, setView] = useState<CapabilityView>("catalog");
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [adminStubState, setAdminStubState] = useState<
    Record<string, AdminStubStatus | undefined>
  >({});
  const [actionLog, setActionLog] = useState<ActionLogEntry[]>([]);
  const [starterDismissed, setStarterDismissed] = useState(false);
  const [togglingSlugs, setTogglingSlugs] = useState<Set<string>>(
    () => new Set(),
  );
  const [connectingSlugs, setConnectingSlugs] = useState<Set<string>>(
    () => new Set(),
  );
  const [scopeDraft, setScopeDraft] = useState("");
  const [scopeDirty, setScopeDirty] = useState(false);
  const [savingScope, setSavingScope] = useState(false);
  const { toast, showToast } = useToast();
  const { setEnd } = usePageHeader();

  useEffect(() => {
    let cancelled = false;
    api
      .getCapabilityToolkits()
      .then((result) => {
        if (cancelled) return;
        setToolkits(result.toolkits);
        setLoadError(null);
      })
      .catch((error: unknown) => {
        if (!cancelled) setLoadError(String(error));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const refreshToolkits = useCallback(async () => {
    setRefreshing(true);
    try {
      const result = await api.getCapabilityToolkits();
      setToolkits(result.toolkits);
      setLoadError(null);
    } catch (error) {
      showToast(`Error: ${error}`, "error");
    } finally {
      setRefreshing(false);
    }
  }, [showToast]);

  const retryInitialLoad = async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const result = await api.getCapabilityToolkits();
      setToolkits(result.toolkits);
    } catch (error) {
      setLoadError(String(error));
    } finally {
      setLoading(false);
    }
  };

  useLayoutEffect(() => {
    setEnd(
      <Button
        ghost
        size="sm"
        className="uppercase"
        onClick={() => void refreshToolkits()}
        disabled={loading || refreshing}
        prefix={refreshing ? <Spinner /> : <RefreshCw />}
      >
        Refresh
      </Button>,
    );
    return () => {
      setEnd(null);
    };
  }, [loading, refreshToolkits, refreshing, setEnd]);

  const mergedToolkits = useMemo(
    () => toolkits.map(mergeCapabilityToolkit),
    [toolkits],
  );

  const categories = useMemo(() => {
    const present = new Set(mergedToolkits.map((toolkit) => toolkit.category));
    const known = CAPABILITY_CATEGORIES.filter(
      (item) => item !== "All" && present.has(item),
    );
    const additional = [...present]
      .filter((item) => !CAPABILITY_CATEGORIES.includes(item))
      .sort((a, b) => a.localeCompare(b));
    return ["All", ...known, ...additional];
  }, [mergedToolkits]);

  const filteredToolkits = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    return mergedToolkits.filter(
      (toolkit) =>
        (category === "All" || toolkit.category === category) &&
        (!query || toolkit.name.toLocaleLowerCase().includes(query)),
    );
  }, [category, mergedToolkits, search]);

  const selectedToolkit = useMemo(
    () => mergedToolkits.find((toolkit) => toolkit.slug === selectedSlug),
    [mergedToolkits, selectedSlug],
  );
  useEffect(() => {
    setScopeDraft(toolScopeDraft(selectedToolkit));
    setScopeDirty(false);
  }, [selectedToolkit?.slug, selectedToolkit?.toolsOverride]);
  const closePanel = useCallback(() => setSelectedSlug(null), []);
  const modalRef = useModalBehavior({
    open: selectedToolkit !== undefined,
    onClose: closePanel,
  });

  const appendAction = useCallback(
    (action: ToolkitAction, toolkitName: string) => {
      const timestamp = new Date();
      setActionLog((previous) => [
        {
          id: `${timestamp.getTime()}-${previous.length}`,
          time: timestamp.toLocaleTimeString(),
          action,
          toolkitName,
        },
        ...previous,
      ]);
    },
    [],
  );

  const handleToggle = async (
    toolkit: MergedCapabilityToolkit,
    enabled: boolean,
  ) => {
    setTogglingSlugs((previous) => new Set(previous).add(toolkit.slug));
    setToolkits((previous) =>
      previous.map((item) =>
        item.slug === toolkit.slug ? { ...item, enabled } : item,
      ),
    );
    try {
      const result = await api.setToolkitEnabled(toolkit.slug, enabled);
      setToolkits((previous) =>
        previous.map((item) =>
          item.slug === toolkit.slug
            ? {
                ...item,
                enabled: result.enabled,
                toolsOverride: result.toolsOverride ?? null,
              }
            : item,
        ),
      );
      appendAction(result.enabled ? "enabled" : "disabled", toolkit.name);
    } catch (error) {
      setToolkits((previous) =>
        previous.map((item) =>
          item.slug === toolkit.slug
            ? { ...item, enabled: toolkit.enabled }
            : item,
        ),
      );
      showToast(`Error: ${error}`, "error");
    } finally {
      setTogglingSlugs((previous) => {
        const next = new Set(previous);
        next.delete(toolkit.slug);
        return next;
      });
    }
  };

  const handleAdminStub = (
    toolkit: MergedCapabilityToolkit,
    status: AdminStubStatus | undefined,
  ) => {
    setAdminStubState((previous) => ({
      ...previous,
      [toolkit.slug]: status,
    }));
    if (status) {
      appendAction(
        status === "soft" ? "soft-disabled" : "revoked",
        toolkit.name,
      );
    }
  };

  const handleConnect = async (toolkit: MergedCapabilityToolkit) => {
    setConnectingSlugs((previous) => new Set(previous).add(toolkit.slug));
    try {
      const result = await api.connectToolkit(toolkit.slug);
      window.open(result.connect_url, "_blank", "noopener,noreferrer");
      showToast(
        "Opened the connect flow — click refresh once you've finished.",
        "success",
      );
    } catch (error) {
      showToast(`Error: ${error}`, "error");
    } finally {
      setConnectingSlugs((previous) => {
        const next = new Set(previous);
        next.delete(toolkit.slug);
        return next;
      });
    }
  };

  const reconcileScope = (slug: string, nextToolkits: CapabilityToolkit[]) => {
    setToolkits(nextToolkits);
    const storedToolkit = nextToolkits.find((toolkit) => toolkit.slug === slug);
    setScopeDraft(toolScopeDraft(storedToolkit));
    setScopeDirty(false);
  };

  const handleSaveScope = async () => {
    if (!selectedToolkit) return;
    const tools = parseToolScope(scopeDraft);
    if (
      tools.length === 0 &&
      !window.confirm(
        "Save an empty scope? This blocks all tools in this toolkit.",
      )
    ) {
      return;
    }

    const { slug, enabled } = selectedToolkit;
    setSavingScope(true);
    try {
      await api.setToolkitScope(slug, { enabled, tools });
      const result = await api.getCapabilityToolkits();
      reconcileScope(slug, result.toolkits);
      showToast("Tool scope saved.", "success");
    } catch (error) {
      showToast(`Error: ${error}`, "error");
    } finally {
      setSavingScope(false);
    }
  };

  const handleClearScope = async () => {
    if (!selectedToolkit) return;
    const { slug, enabled } = selectedToolkit;
    setSavingScope(true);
    try {
      // Explicit `tools: null` is the clear-override signal: NAS treats an
      // absent `tools` field as "preserve the existing scope" (that's what
      // the enable/disable toggle relies on), so clearing must send null
      // rather than omit the field.
      await api.setToolkitScope(slug, { enabled, tools: null });
      const result = await api.getCapabilityToolkits();
      reconcileScope(slug, result.toolkits);
      showToast("Tool scope removed. All tools are allowed.", "success");
    } catch (error) {
      showToast(`Error: ${error}`, "error");
    } finally {
      setSavingScope(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="flex flex-col gap-6">
        <Toast toast={toast} />
        <Card className="rounded-none">
          <CardContent className="flex flex-col items-center gap-3 py-10 text-center">
            <p className="text-sm text-destructive">
              Could not load app toolkits: {loadError}
            </p>
            <Button size="sm" onClick={() => void retryInitialLoad()}>
              Retry
            </Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-10">
      <Toast toast={toast} />

      <section className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <H2
            variant="sm"
            className="flex items-center gap-2 text-muted-foreground"
          >
            <Blocks className="h-4 w-4" />
            App toolkits ({mergedToolkits.length})
          </H2>
          <div
            className="inline-flex rounded-full border border-border bg-muted/50 p-0.5"
            role="tablist"
            aria-label="Toolkit view"
          >
            {(["catalog", "admin"] as const).map((item) => (
              <button
                key={item}
                type="button"
                role="tab"
                aria-selected={view === item}
                onClick={() => setView(item)}
                className={cn(
                  "rounded-full px-3 py-1 text-xs capitalize transition-colors",
                  view === item
                    ? "bg-foreground text-background"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {item}
              </button>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
          <div className="min-w-0 space-y-4">
            {view === "catalog" &&
              mergedToolkits.every((toolkit) => !toolkit.enabled) &&
              !starterDismissed && (
                <StarterBanner
                  toolkits={mergedToolkits}
                  togglingSlugs={togglingSlugs}
                  onEnable={handleToggle}
                  onDismiss={() => setStarterDismissed(true)}
                />
              )}

            <div className="flex flex-col gap-3">
              <div className="relative max-w-xl">
                <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="Search toolkits"
                  aria-label="Search toolkits"
                  className="pl-9"
                />
              </div>

              <div
                className="flex flex-wrap gap-2"
                aria-label="Toolkit categories"
              >
                {categories.map((item) => (
                  <button
                    key={item}
                    type="button"
                    onClick={() => setCategory(item)}
                    className={cn(
                      "border px-3 py-1 text-xs transition-colors",
                      category === item
                        ? "border-foreground/30 bg-foreground text-background"
                        : "border-border bg-card text-muted-foreground hover:bg-muted hover:text-foreground",
                    )}
                  >
                    {item}
                  </button>
                ))}
              </div>
            </div>

            {view === "catalog" ? (
              <CatalogGrid
                toolkits={filteredToolkits}
                togglingSlugs={togglingSlugs}
                connectingSlugs={connectingSlugs}
                onSelect={setSelectedSlug}
                onToggle={handleToggle}
                onConnect={handleConnect}
              />
            ) : (
              <div className="space-y-4">
                <AdminTable
                  toolkits={filteredToolkits}
                  togglingSlugs={togglingSlugs}
                  stubState={adminStubState}
                  onToggle={handleToggle}
                  onStubChange={handleAdminStub}
                />
                <ActionLog entries={actionLog} />
              </div>
            )}
          </div>

          <AgentPreviewSidebar toolkits={mergedToolkits} />
        </div>
      </section>

      <McpServersSection />

      {selectedToolkit && (
        <div
          ref={modalRef}
          className="fixed inset-0 z-[100] flex justify-end bg-background/85"
          onClick={(event) =>
            event.target === event.currentTarget && closePanel()
          }
          role="dialog"
          aria-modal="true"
          aria-labelledby="capability-panel-title"
        >
          <div
            className={cn(
              themedBody,
              "relative flex h-full w-full max-w-md flex-col border-l border-border bg-card shadow-2xl",
            )}
          >
            <Button
              ghost
              size="icon"
              onClick={closePanel}
              className="absolute right-2 top-2 text-muted-foreground hover:text-foreground"
              aria-label="Close"
            >
              <X />
            </Button>

            <header className="flex items-center gap-3 border-b border-border p-5 pr-14">
              <BrandGlyph toolkit={selectedToolkit} size="lg" />
              <div className="min-w-0">
                <h2
                  id="capability-panel-title"
                  className="truncate font-mondwest text-base tracking-wider text-foreground"
                >
                  {selectedToolkit.name}
                </h2>
                <Badge tone="outline" className="mt-1">
                  {selectedToolkit.category}
                </Badge>
              </div>
            </header>

            <div className="flex flex-1 flex-col gap-6 overflow-y-auto p-5">
              {selectedToolkit.description && (
                <p className="text-sm leading-6 text-muted-foreground">
                  {selectedToolkit.description}
                </p>
              )}

              <div className="flex flex-wrap items-center gap-2 border-y border-border py-4">
                <ToolkitStatus toolkit={selectedToolkit} />
                <div className="ml-auto flex items-center gap-2">
                  <Button
                    ghost
                    size="sm"
                    onClick={() => void refreshToolkits()}
                    disabled={refreshing}
                    prefix={refreshing ? <Spinner /> : <RefreshCw />}
                  >
                    Refresh
                  </Button>
                  <Button
                    size="sm"
                    disabled={
                      !selectedToolkit.enabled ||
                      connectingSlugs.has(selectedToolkit.slug)
                    }
                    title={
                      selectedToolkit.enabled
                        ? "Connect this toolkit"
                        : "Enable this toolkit first"
                    }
                    prefix={
                      connectingSlugs.has(selectedToolkit.slug) ? (
                        <Spinner />
                      ) : (
                        <ExternalLink />
                      )
                    }
                    onClick={() => void handleConnect(selectedToolkit)}
                  >
                    Connect
                  </Button>
                </div>
              </div>

              <div className="flex flex-col gap-3">
                <H2 variant="sm" className="text-muted-foreground">
                  Recommended tools
                </H2>
                {RECOMMENDED_TOOLS[selectedToolkit.slug] ? (
                  <div className="divide-y divide-border border border-border">
                    {RECOMMENDED_TOOLS[selectedToolkit.slug].map((tool) => (
                      <div key={tool.slug} className="flex gap-3 p-3">
                        <Wrench className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                        <div className="min-w-0">
                          <p className="text-sm font-medium text-foreground">
                            {tool.name}
                          </p>
                          <p className="mt-1 text-xs leading-5 text-muted-foreground">
                            {tool.desc}
                          </p>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    Tool list preview not available for this app yet.
                  </p>
                )}
              </div>

              <div className="flex flex-col gap-3 border-t border-border pt-6">
                <div className="flex flex-wrap items-center gap-2">
                  <H2 variant="sm" className="text-muted-foreground">
                    Tool scope
                  </H2>
                  {selectedToolkit.toolsOverride == null ? (
                    <Badge tone="outline">All tools allowed</Badge>
                  ) : selectedToolkit.toolsOverride.length > 0 ? (
                    <Badge tone="secondary">
                      Scoped to {selectedToolkit.toolsOverride.length} tools
                    </Badge>
                  ) : (
                    <Badge tone="warning">
                      Scoped to none — all tools blocked
                    </Badge>
                  )}
                </div>
                {selectedToolkit.toolsOverride?.length === 0 && (
                  <p className="text-xs text-warning">
                    This explicit empty scope blocks every tool in the toolkit.
                  </p>
                )}
                <p className="text-sm text-muted-foreground">
                  Enter tool slugs separated by commas or new lines.
                </p>
                {/* NAS exposes a toolkit catalog, not a tool catalog, so there is no source for a picker until a tool-listing endpoint exists. */}
                <textarea
                  aria-label="Tool slugs"
                  className="flex min-h-[96px] w-full border border-border bg-background/40 px-3 py-2 text-sm font-courier shadow-sm placeholder:text-muted-foreground focus-visible:border-foreground/25 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-foreground/30"
                  placeholder={"GITHUB_GET_REPOS\nGITHUB_GET_ISSUES"}
                  value={scopeDraft}
                  onChange={(event) => {
                    setScopeDraft(event.target.value);
                    setScopeDirty(true);
                  }}
                />
                {parseToolScope(scopeDraft).length === 0 && (
                  <p className="text-xs text-muted-foreground">
                    Saving this empty scope will block all tools and requires
                    confirmation.
                  </p>
                )}
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    size="sm"
                    disabled={savingScope}
                    prefix={savingScope ? <Spinner /> : undefined}
                    onClick={() => void handleSaveScope()}
                  >
                    Save scope
                  </Button>
                  <Button
                    ghost
                    size="sm"
                    disabled={
                      savingScope || selectedToolkit.toolsOverride == null
                    }
                    onClick={() => void handleClearScope()}
                  >
                    Clear override
                  </Button>
                  <span className="text-xs text-muted-foreground">
                    {scopeDirty ? "Unsaved changes" : "Matches saved scope"}
                  </span>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

interface CatalogGridProps {
  toolkits: MergedCapabilityToolkit[];
  togglingSlugs: Set<string>;
  connectingSlugs: Set<string>;
  onSelect: (slug: string) => void;
  onToggle: (toolkit: MergedCapabilityToolkit, enabled: boolean) => void;
  onConnect: (toolkit: MergedCapabilityToolkit) => void;
}

function CatalogGrid({
  toolkits,
  togglingSlugs,
  connectingSlugs,
  onSelect,
  onToggle,
  onConnect,
}: CatalogGridProps) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {toolkits.length === 0 ? (
        <div className="col-span-full border border-dashed border-border py-10 text-center text-sm text-muted-foreground">
          No toolkits match.
        </div>
      ) : (
        toolkits.map((toolkit) => {
          const toggling = togglingSlugs.has(toolkit.slug);
          const connecting = connectingSlugs.has(toolkit.slug);
          return (
            <Card
              key={toolkit.slug}
              className={cn(
                "relative cursor-pointer rounded-none transition-colors hover:border-foreground/30",
                !toolkit.enabled && "opacity-75",
              )}
              onClick={() => onSelect(toolkit.slug)}
            >
              <CardContent className="flex h-full flex-col gap-4 p-4">
                <div className="flex items-start gap-3">
                  <BrandGlyph toolkit={toolkit} />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-foreground">
                      {toolkit.name}
                    </p>
                    <Badge tone="outline" className="mt-1">
                      {toolkit.category}
                    </Badge>
                  </div>
                </div>

                {toolkit.description && (
                  <p className="min-h-8 text-xs leading-5 text-muted-foreground">
                    {toolkit.description}
                  </p>
                )}

                <div className="mt-auto flex items-center justify-between gap-2 border-t border-border pt-3">
                  <ToolkitStatus toolkit={toolkit} />
                  <div className="flex items-center gap-2">
                    {toolkit.enabled && (
                      <Button
                        ghost
                        size="sm"
                        disabled={connecting}
                        prefix={connecting ? <Spinner /> : <ExternalLink />}
                        onClick={(event) => {
                          event.stopPropagation();
                          onConnect(toolkit);
                        }}
                      >
                        Connect
                      </Button>
                    )}
                    <div onClick={(event) => event.stopPropagation()}>
                      <Switch
                        checked={toolkit.enabled}
                        disabled={toggling}
                        aria-label={`${toolkit.enabled ? "Disable" : "Enable"} ${toolkit.name}`}
                        onCheckedChange={(next) => onToggle(toolkit, next)}
                      />
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>
          );
        })
      )}
    </div>
  );
}

interface StarterBannerProps {
  toolkits: MergedCapabilityToolkit[];
  togglingSlugs: Set<string>;
  onEnable: (toolkit: MergedCapabilityToolkit, enabled: boolean) => void;
  onDismiss: () => void;
}

function StarterBanner({
  toolkits,
  togglingSlugs,
  onEnable,
  onDismiss,
}: StarterBannerProps) {
  const starters = STARTER_TOOLKIT_SLUGS.map((slug) =>
    toolkits.find((toolkit) => toolkit.slug === slug),
  ).filter((toolkit): toolkit is MergedCapabilityToolkit => Boolean(toolkit));

  if (starters.length === 0) return null;

  return (
    <div className="relative border border-border bg-muted/30 p-4 pr-12">
      <Button
        ghost
        size="icon"
        className="absolute right-1 top-1 text-muted-foreground"
        onClick={onDismiss}
        aria-label="Dismiss starter pack"
      >
        <X />
      </Button>
      <p className="text-sm font-medium text-foreground">Get started</p>
      <p className="mt-1 text-xs leading-5 text-muted-foreground">
        Enable a starter toolkit now, then connect it to begin using its tools.
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        {starters.map((toolkit) => (
          <Button
            key={toolkit.slug}
            ghost
            size="sm"
            className="rounded-full border border-border bg-card"
            disabled={togglingSlugs.has(toolkit.slug)}
            onClick={() => onEnable(toolkit, true)}
          >
            {toolkit.name}
          </Button>
        ))}
      </div>
      {/* A production starter affordance could persist dismissal across visits. */}
    </div>
  );
}

interface AdminTableProps {
  toolkits: MergedCapabilityToolkit[];
  togglingSlugs: Set<string>;
  stubState: Record<string, AdminStubStatus | undefined>;
  onToggle: (toolkit: MergedCapabilityToolkit, enabled: boolean) => void;
  onStubChange: (
    toolkit: MergedCapabilityToolkit,
    status: AdminStubStatus | undefined,
  ) => void;
}

function AdminTable({
  toolkits,
  togglingSlugs,
  stubState,
  onToggle,
  onStubChange,
}: AdminTableProps) {
  return (
    <div className="space-y-2">
      <p className="text-xs italic text-muted-foreground">
        Soft-disable and Revoke are prototype-only — they don&apos;t call the
        Nous backend. Enable/disable is real.
      </p>
      <div className="overflow-x-auto border border-border">
        <table className="w-full border-collapse text-sm">
          <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
            <tr className="border-b border-border">
              <th className="px-3 py-2 font-medium">Toolkit</th>
              <th className="px-3 py-2 font-medium">Category</th>
              <th className="px-3 py-2 font-medium">Status</th>
              <th className="px-3 py-2 text-center font-medium">Enabled</th>
              <th className="px-3 py-2 font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {toolkits.length === 0 ? (
              <tr>
                <td
                  colSpan={5}
                  className="px-3 py-10 text-center text-muted-foreground"
                >
                  No toolkits match.
                </td>
              </tr>
            ) : (
              toolkits.map((toolkit) => {
                const stubStatus = stubState[toolkit.slug];
                return (
                  <tr key={toolkit.slug} className="border-b border-border">
                    <td className="px-3 py-3">
                      <div className="flex min-w-40 items-center gap-2">
                        <BrandGlyph toolkit={toolkit} />
                        <span className="font-medium text-foreground">
                          {toolkit.name}
                        </span>
                      </div>
                    </td>
                    <td className="px-3 py-3 text-muted-foreground">
                      {toolkit.category}
                    </td>
                    <td className="px-3 py-3">
                      <ToolkitStatus toolkit={toolkit} />
                    </td>
                    <td className="px-3 py-3 text-center">
                      <Switch
                        checked={toolkit.enabled}
                        disabled={
                          togglingSlugs.has(toolkit.slug) ||
                          stubStatus !== undefined
                        }
                        aria-label={`${toolkit.enabled ? "Disable" : "Enable"} ${toolkit.name}`}
                        onCheckedChange={(next) => onToggle(toolkit, next)}
                      />
                    </td>
                    <td className="px-3 py-3">
                      <div className="flex min-w-max flex-wrap items-center gap-1">
                        <Button
                          ghost
                          size="sm"
                          onClick={() => onStubChange(toolkit, "soft")}
                        >
                          Soft-disable
                        </Button>
                        <Button
                          ghost
                          size="sm"
                          className="text-destructive hover:text-destructive"
                          onClick={() => onStubChange(toolkit, "revoked")}
                        >
                          Revoke
                        </Button>
                        {stubStatus === "soft" && (
                          <Badge tone="warning">soft-disabled</Badge>
                        )}
                        {stubStatus === "revoked" && (
                          <Badge tone="destructive">revoked</Badge>
                        )}
                        {stubStatus !== undefined && (
                          <Button
                            ghost
                            size="sm"
                            onClick={() => onStubChange(toolkit, undefined)}
                          >
                            Clear
                          </Button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ActionLog({ entries }: { entries: ActionLogEntry[] }) {
  return (
    <div className="border border-border bg-card">
      <div className="border-b border-border px-3 py-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
        Action log
      </div>
      <div className="max-h-56 overflow-y-auto p-3 font-mono text-xs">
        {entries.length === 0 ? (
          <p className="text-muted-foreground">No actions this session.</p>
        ) : (
          <ol className="space-y-2">
            {entries.map((entry) => (
              <li key={entry.id}>
                <span className="text-muted-foreground">{entry.time}</span>
                {" — "}
                {entry.action} {entry.toolkitName}
              </li>
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}

function AgentPreviewSidebar({
  toolkits,
}: {
  toolkits: MergedCapabilityToolkit[];
}) {
  const enabledToolkits = toolkits.filter((toolkit) => toolkit.enabled);
  const budgetPercent = Math.min((enabledToolkits.length / 12) * 100, 100);

  return (
    <aside className="h-fit border border-border bg-card p-4 xl:sticky xl:top-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs uppercase tracking-wide text-muted-foreground">
          Agent preview
        </p>
        <Badge tone="outline">Prototype — not wired</Badge>
      </div>

      <div className="mt-5 border-b border-border pb-4">
        <p className="text-xs text-muted-foreground">Mode</p>
        <Badge
          tone={enabledToolkits.length > 0 ? "secondary" : "outline"}
          className="mt-2"
        >
          {enabledToolkits.length > 0 ? "Search + connect" : "No tools"}
        </Badge>
      </div>

      <div className="border-b border-border py-4">
        <p className="text-xs font-medium text-foreground">
          Enabled toolkits ({enabledToolkits.length})
        </p>
        {enabledToolkits.length === 0 ? (
          <p className="mt-3 text-xs leading-5 text-muted-foreground">
            Enable a toolkit to see it represented here.
          </p>
        ) : (
          <ul className="mt-3 divide-y divide-border">
            {enabledToolkits.map((toolkit) => {
              // Illustrative placeholder only; this is not a measured token count.
              const tokenEstimate = 40 + toolkit.name.length;
              return (
                <li
                  key={toolkit.slug}
                  className="flex items-center justify-between gap-3 py-2 text-xs"
                >
                  <span className="truncate text-foreground">
                    {toolkit.name}
                  </span>
                  <span className="shrink-0 text-muted-foreground">
                    ~{tokenEstimate} tokens
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <div className="pt-4">
        <div className="flex items-center justify-between gap-3 text-xs">
          <span className="font-medium text-foreground">Budget preview</span>
          <span className="text-muted-foreground">
            {enabledToolkits.length}/12
          </span>
        </div>
        <div className="mt-2 h-2 overflow-hidden rounded-full bg-muted">
          <div
            className="h-full bg-primary transition-[width]"
            style={{ width: `${budgetPercent}%` }}
          />
        </div>
        <p className="mt-2 text-xs leading-5 text-muted-foreground">
          Illustrative — real budget accounting isn&apos;t wired yet.
        </p>
      </div>
    </aside>
  );
}
