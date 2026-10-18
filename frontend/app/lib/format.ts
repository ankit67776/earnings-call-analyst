import type { ClaimType, MatchKind } from "./types";

// EDGAR names come in capitals ("HERSHEY CO"); show them the way people write them.
const KEEP_UPPER = new Set(["LLC", "LP", "PLC", "NV", "SA", "AG", "USA", "II", "III"]);

export function companyName(name: string): string {
  return name
    .split(/\s+/)
    .map((word) =>
      KEEP_UPPER.has(word.toUpperCase()) || word.length <= 1
        ? word.toUpperCase()
        : word[0].toUpperCase() + word.slice(1).toLowerCase(),
    )
    .join(" ");
}

const ITEM_NAMES: Record<string, string> = {
  "1": "Business",
  "1A": "Risk factors",
  "7": "Management's discussion and analysis",
  "7A": "Market risk",
};

// For tabs and tight rows.
const ITEM_SHORT: Record<string, string> = { "7": "MD&A", "7A": "Market risk" };

export function sectionShort(item: string, title?: string): string {
  return ITEM_SHORT[item] ?? sectionName(item, title);
}

export function sectionName(item: string, title?: string): string {
  if (ITEM_NAMES[item]) return ITEM_NAMES[item];
  if (!title) return `Item ${item}`;
  const lower = title.toLowerCase();
  return lower[0].toUpperCase() + lower.slice(1);
}

export const CLAIM_LABELS: Record<ClaimType, string> = {
  metric: "Metric",
  guidance: "Guidance",
  risk: "Risk",
  event: "Event",
};

export const MATCH_LABELS: Record<MatchKind, string> = {
  exact: "Exact quote",
  normalized: "Exact apart from punctuation or spacing",
  elided: "Shortened quote, every part found in order",
  fuzzy: "Close match, small copying slips",
};

// How a quote differed from the filing, said briefly; exact quotes need no note.
export const MATCH_NOTES: Record<MatchKind, string | null> = {
  exact: null,
  normalized: "punctuation differs",
  elided: "shortened quote",
  fuzzy: "close match",
};

// "5:30 AM", "tomorrow 5:30 AM" or "Oct 21, 2026, 5:30 AM".
export function clock(iso: string, now = new Date()): string {
  const at = new Date(iso);
  const time = at.toLocaleTimeString("en-US", { timeStyle: "short" });
  const tomorrow = new Date(now);
  tomorrow.setDate(now.getDate() + 1);
  if (at.toDateString() === now.toDateString()) return time;
  if (at.toDateString() === tomorrow.toDateString()) return `tomorrow ${time}`;
  return at.toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" });
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;
}
