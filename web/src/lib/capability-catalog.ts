import type { CapabilityToolkit } from "./api";

export interface CapabilityCatalogEntry {
  slug: string;
  name: string;
  category: string;
  brand: string;
  description: string;
}

export const CAPABILITY_CATALOG: CapabilityCatalogEntry[] = [
  {
    slug: "gmail",
    name: "Gmail",
    category: "Communication",
    brand: "#EA4335",
    description: "Send & manage email.",
  },
  {
    slug: "slack",
    name: "Slack",
    category: "Communication",
    brand: "#611F69",
    description: "Channels, DMs, messages.",
  },
  {
    slug: "github",
    name: "GitHub",
    category: "Dev Tools",
    brand: "#0A0A0A",
    description: "Repos, PRs, issues, code.",
  },
  {
    slug: "linear",
    name: "Linear",
    category: "Project Mgmt",
    brand: "#5E6AD2",
    description: "Issues, projects, cycles.",
  },
  {
    slug: "notion",
    name: "Notion",
    category: "Productivity",
    brand: "#0A0A0A",
    description: "Pages, databases, blocks.",
  },
  {
    slug: "googlecalendar",
    name: "Google Calendar",
    category: "Productivity",
    brand: "#4285F4",
    description: "Events & scheduling.",
  },
  {
    slug: "googlesheets",
    name: "Google Sheets",
    category: "Productivity",
    brand: "#0F9D58",
    description: "Spreadsheets, ranges, formulas.",
  },
  {
    slug: "googledocs",
    name: "Google Docs",
    category: "Productivity",
    brand: "#4285F4",
    description: "Documents & comments.",
  },
  {
    slug: "googledrive",
    name: "Google Drive",
    category: "Productivity",
    brand: "#FBBC04",
    description: "Files, folders, sharing.",
  },
  {
    slug: "outlook",
    name: "Outlook",
    category: "Communication",
    brand: "#0078D4",
    description: "Email & calendar.",
  },
  {
    slug: "jira",
    name: "Jira",
    category: "Project Mgmt",
    brand: "#0052CC",
    description: "Issues, sprints, boards.",
  },
  {
    slug: "hubspot",
    name: "HubSpot",
    category: "CRM",
    brand: "#FF7A59",
    description: "Contacts, deals, marketing.",
  },
  {
    slug: "salesforce",
    name: "Salesforce",
    category: "CRM",
    brand: "#00A1E0",
    description: "Accounts, opportunities, leads.",
  },
  {
    slug: "airtable",
    name: "Airtable",
    category: "Productivity",
    brand: "#F82B60",
    description: "Bases, tables, records.",
  },
  {
    slug: "discord",
    name: "Discord",
    category: "Communication",
    brand: "#5865F2",
    description: "Servers, channels, messages.",
  },
  {
    slug: "twitter",
    name: "X (Twitter)",
    category: "Social",
    brand: "#0A0A0A",
    description: "Tweets, DMs, followers.",
  },
  {
    slug: "youtube",
    name: "YouTube",
    category: "Social",
    brand: "#FF0000",
    description: "Videos, channels, comments.",
  },
  {
    slug: "supabase",
    name: "Supabase",
    category: "Dev Tools",
    brand: "#3ECF8E",
    description: "Postgres, auth, storage.",
  },
  {
    slug: "firecrawl",
    name: "Firecrawl",
    category: "Web & Search",
    brand: "#FF6A00",
    description: "Crawl & extract pages.",
  },
  {
    slug: "perplexityai",
    name: "Perplexity",
    category: "Web & Search",
    brand: "#1F8FAE",
    description: "Search & answer.",
  },
  {
    slug: "serpapi",
    name: "SerpAPI",
    category: "Web & Search",
    brand: "#6A48F7",
    description: "Google search results.",
  },
  {
    slug: "tavily",
    name: "Tavily",
    category: "Web & Search",
    brand: "#3B82F6",
    description: "AI-native web search.",
  },
  {
    slug: "codeinterpreter",
    name: "Code Interpreter",
    category: "Dev Tools",
    brand: "#1B40FF",
    description: "Run code in a sandbox.",
  },
  {
    slug: "stripe",
    name: "Stripe",
    category: "Finance",
    brand: "#635BFF",
    description: "Payments, customers, invoices.",
  },
  {
    slug: "intercom",
    name: "Intercom",
    category: "Support",
    brand: "#1F8DED",
    description: "Conversations, contacts.",
  },
  {
    slug: "zendesk",
    name: "Zendesk",
    category: "Support",
    brand: "#03363D",
    description: "Tickets, users, macros.",
  },
  {
    slug: "asana",
    name: "Asana",
    category: "Project Mgmt",
    brand: "#F06A6A",
    description: "Tasks & projects.",
  },
  {
    slug: "trello",
    name: "Trello",
    category: "Project Mgmt",
    brand: "#0079BF",
    description: "Boards & cards.",
  },
  {
    slug: "confluence",
    name: "Confluence",
    category: "Productivity",
    brand: "#172B4D",
    description: "Wiki pages, spaces.",
  },
  {
    slug: "dropbox",
    name: "Dropbox",
    category: "Productivity",
    brand: "#0061FF",
    description: "Files & folders.",
  },
  {
    slug: "figma",
    name: "Figma",
    category: "Design",
    brand: "#F24E1E",
    description: "Files, frames, comments.",
  },
  {
    slug: "monday",
    name: "Monday.com",
    category: "Project Mgmt",
    brand: "#FF3D57",
    description: "Boards & workflows.",
  },
  {
    slug: "shopify",
    name: "Shopify",
    category: "Finance",
    brand: "#96BF48",
    description: "Products, orders, customers.",
  },
];

export const FALLBACK_TOOLKIT_BRAND = "#64748B";

export interface MergedCapabilityToolkit extends CapabilityToolkit {
  category: string;
  brand: string;
  description: string;
  toolsOverride?: string[] | null;
}

const catalogBySlug = new Map(
  CAPABILITY_CATALOG.map((entry) => [entry.slug, entry]),
);

// The tool-provider vendor must never be named on any user-visible surface.
// Server-supplied free-text fields (description, etc.) come from the
// vendor's own live catalog and can contain the vendor's name spelled out
// (see toolkit `browser_tool`, whose description begins with it). Scrub any
// mention before it reaches the DOM, redacting rather than blanking so the
// sentence still reads naturally.
const VENDOR_NAME_PATTERN = /composio/gi;
const VENDOR_REPLACEMENT = "the tool provider";

/**
 * Redact vendor-name mentions from a single free-text catalog field. Applied
 * to every prose field that can originate from the vendor's live catalog, so
 * a future field cannot bypass the scrub by rendering directly.
 */
export function scrubVendorMentions(text: string): string {
  return text.replace(VENDOR_NAME_PATTERN, VENDOR_REPLACEMENT);
}

/**
 * Enrich a server-supplied toolkit with local presentation data (brand color,
 * category, description) where this repo's curated catalog has an entry for
 * its slug. The server catalog (NAS's live top-100-by-usage catalog list) is
 * the source of truth for WHICH toolkits exist; this local catalog is
 * enrichment only, for a subset of well-known slugs.
 *
 * - brand: local catalog's brand color if the slug matches, else a neutral
 *   fallback (no server equivalent exists).
 * - category: local catalog's category if the slug matches (it's the more
 *   curated/user-facing label); otherwise the server's own category if it
 *   provided one; otherwise "Other".
 * - description: the server's description when present (it's the richer,
 *   live-fetched copy); otherwise the local catalog's short blurb if the
 *   slug matches; otherwise an empty string. Always scrubbed of vendor
 *   mentions before it reaches callers, since it can carry raw vendor
 *   catalog prose.
 */
export function mergeCapabilityToolkit(
  toolkit: CapabilityToolkit,
): MergedCapabilityToolkit {
  const catalogEntry = catalogBySlug.get(toolkit.slug);
  const description = toolkit.description ?? catalogEntry?.description ?? "";
  // `category` has the same provenance as `description` — the server forwards it straight from the
  // upstream catalog — so it gets the same scrub. Every server-supplied free-text field that
  // reaches the DOM must pass through here; a local curated value is safe but is scrubbed anyway
  // rather than branching, so a future field cannot be added on the unscrubbed path by accident.
  const category = catalogEntry?.category ?? toolkit.category ?? "Other";
  return {
    ...toolkit,
    category: scrubVendorMentions(category),
    brand: catalogEntry?.brand ?? FALLBACK_TOOLKIT_BRAND,
    description: scrubVendorMentions(description),
  };
}

export const CAPABILITY_CATEGORIES: string[] = [
  "All",
  "Communication",
  "Dev Tools",
  "Productivity",
  "Project Mgmt",
  "CRM",
  "Web & Search",
  "Social",
  "Finance",
  "Support",
  "Design",
];

export interface RecommendedTool {
  slug: string;
  name: string;
  desc: string;
}

export const RECOMMENDED_TOOLS: Record<string, RecommendedTool[]> = {
  gmail: [
    {
      slug: "GMAIL_SEND_EMAIL",
      name: "Send email",
      desc: "Compose and send a new email.",
    },
    {
      slug: "GMAIL_LIST_THREADS",
      name: "List threads",
      desc: "Fetch recent threads with filters.",
    },
    {
      slug: "GMAIL_FETCH_EMAILS",
      name: "Fetch emails",
      desc: "Read message bodies by query.",
    },
    {
      slug: "GMAIL_REPLY_TO_THREAD",
      name: "Reply to thread",
      desc: "Reply in-line on a thread.",
    },
    {
      slug: "GMAIL_CREATE_DRAFT",
      name: "Create draft",
      desc: "Save a draft without sending.",
    },
    {
      slug: "GMAIL_ADD_LABEL",
      name: "Add label",
      desc: "Apply a label to a message.",
    },
    {
      slug: "GMAIL_SEARCH",
      name: "Search messages",
      desc: "Full-text Gmail search.",
    },
  ],
  slack: [
    {
      slug: "SLACK_SEND_MESSAGE",
      name: "Send message",
      desc: "Post to a channel or user.",
    },
    {
      slug: "SLACK_LIST_CHANNELS",
      name: "List channels",
      desc: "All channels in the workspace.",
    },
    {
      slug: "SLACK_FETCH_HISTORY",
      name: "Fetch history",
      desc: "Recent messages from a channel.",
    },
    {
      slug: "SLACK_REPLY_THREAD",
      name: "Reply in thread",
      desc: "Reply inside a thread.",
    },
    {
      slug: "SLACK_ADD_REACTION",
      name: "Add reaction",
      desc: "Emoji react to a message.",
    },
    {
      slug: "SLACK_LIST_USERS",
      name: "List users",
      desc: "Members of the workspace.",
    },
    {
      slug: "SLACK_UPLOAD_FILE",
      name: "Upload file",
      desc: "Share a file to a channel.",
    },
  ],
  github: [
    {
      slug: "GITHUB_CREATE_ISSUE",
      name: "Create issue",
      desc: "Open a new issue in a repo.",
    },
    {
      slug: "GITHUB_LIST_PRS",
      name: "List pull requests",
      desc: "PRs in a repo, filtered.",
    },
    {
      slug: "GITHUB_COMMENT_ON_ISSUE",
      name: "Comment on issue",
      desc: "Reply on an issue or PR.",
    },
    {
      slug: "GITHUB_GET_REPO_CONTENT",
      name: "Get repo content",
      desc: "Read a file or dir.",
    },
    {
      slug: "GITHUB_SEARCH_CODE",
      name: "Search code",
      desc: "Code search across repos.",
    },
    {
      slug: "GITHUB_CREATE_BRANCH",
      name: "Create branch",
      desc: "Branch off a base ref.",
    },
    {
      slug: "GITHUB_OPEN_PR",
      name: "Open pull request",
      desc: "File a PR against base.",
    },
  ],
  linear: [
    {
      slug: "LINEAR_CREATE_ISSUE",
      name: "Create issue",
      desc: "New issue with title, body, team.",
    },
    {
      slug: "LINEAR_LIST_ISSUES",
      name: "List issues",
      desc: "Filter by status, assignee, team.",
    },
    {
      slug: "LINEAR_UPDATE_ISSUE",
      name: "Update issue",
      desc: "Change status, assignee, priority.",
    },
    {
      slug: "LINEAR_LIST_PROJECTS",
      name: "List projects",
      desc: "Projects in a team.",
    },
    {
      slug: "LINEAR_ADD_COMMENT",
      name: "Add comment",
      desc: "Comment on an issue.",
    },
    {
      slug: "LINEAR_LIST_TEAMS",
      name: "List teams",
      desc: "All teams in the workspace.",
    },
  ],
  notion: [
    {
      slug: "NOTION_CREATE_PAGE",
      name: "Create page",
      desc: "New page in a database/parent.",
    },
    {
      slug: "NOTION_QUERY_DATABASE",
      name: "Query database",
      desc: "Filter & sort database rows.",
    },
    {
      slug: "NOTION_UPDATE_PAGE",
      name: "Update page",
      desc: "Edit properties or content.",
    },
    {
      slug: "NOTION_GET_PAGE",
      name: "Get page",
      desc: "Fetch a page and its blocks.",
    },
    {
      slug: "NOTION_APPEND_BLOCKS",
      name: "Append blocks",
      desc: "Add content to a page.",
    },
  ],
  googlecalendar: [
    {
      slug: "GCAL_CREATE_EVENT",
      name: "Create event",
      desc: "Schedule a new event.",
    },
    {
      slug: "GCAL_LIST_EVENTS",
      name: "List events",
      desc: "Events in a window, calendar.",
    },
    {
      slug: "GCAL_UPDATE_EVENT",
      name: "Update event",
      desc: "Reschedule or edit.",
    },
    {
      slug: "GCAL_DELETE_EVENT",
      name: "Delete event",
      desc: "Cancel an event.",
    },
    {
      slug: "GCAL_FIND_FREE",
      name: "Find free time",
      desc: "Free/busy across calendars.",
    },
  ],
};
