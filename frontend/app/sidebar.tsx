"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { plural } from "./lib/format";
import { useLive } from "./lib/live";
import { type Row, type Status, libraryRows } from "./lib/status";
import { Clock } from "./lib/time";
import type { Usage } from "./lib/types";

const REPO = "https://github.com/ankit67776/earnings-call-analyst";

export function BrandMark({ className = "size-5" }: { className?: string }) {
  // A page with one line run through the highlighter.
  return (
    <svg viewBox="0 0 20 20" aria-hidden className={className}>
      <rect width="20" height="20" rx="5" fill="var(--color-ink)" />
      <rect x="4.5" y="5" width="11" height="1.5" rx="0.75" fill="#fff" opacity="0.5" />
      <rect x="3.5" y="8.5" width="13" height="4" rx="1" fill="var(--color-mark)" />
      <rect x="4.5" y="14.5" width="8" height="1.5" rx="0.75" fill="#fff" opacity="0.5" />
    </svg>
  );
}

export function StatusDot({ status }: { status: Status }) {
  const tone: Record<Status["kind"], string> = {
    ready: "bg-ok",
    empty: "bg-line-strong",
    queued: "bg-ink-3",
    fetching: "bg-accent pulse",
    extracting: "bg-accent pulse",
    waiting: "bg-warn",
    failed: "bg-bad",
  };
  return <span aria-hidden className={`inline-block size-[7px] shrink-0 rounded-full ${tone[status.kind]}`} />;
}

const SHORT_STATUS: Record<Status["kind"], string> = {
  ready: "",
  empty: "not analyzed",
  queued: "queued",
  fetching: "running",
  extracting: "running",
  waiting: "waiting",
  failed: "failed",
};

export default function Sidebar() {
  const pathname = usePathname();
  const { documents, jobs, usage, offline } = useLive();
  const rows = libraryRows(documents ?? [], jobs);
  const running = rows.filter((r) => r.status.kind === "fetching" || r.status.kind === "extracting");

  return (
    <aside className="flex items-center gap-4 border-b border-line bg-rail px-4 py-2.5 lg:flex-col lg:items-stretch lg:gap-0 lg:border-r lg:border-b-0 lg:px-2.5 lg:py-3.5">
      <Link
        href="/"
        className="flex items-center gap-2 rounded-md px-2 py-1 font-semibold tracking-[-0.01em] lg:mb-3"
      >
        <BrandMark />
        Earnings Call Analyst
      </Link>

      <nav aria-label="Main" className="hidden lg:block">
        <NavLink href="/" current={pathname === "/"}>
          Library
          <span className="font-mono text-2xs text-ink-2 tabular-nums">
            {running.length > 0 ? `${running.length} running` : (documents?.length ?? "")}
          </span>
        </NavLink>
      </nav>

      {rows.length > 0 && (
        <div className="hidden lg:block">
          <h2 className="px-2 pt-5 pb-1.5 text-xs text-ink-2">Filings</h2>
          <ul className="space-y-px">
            {rows.slice(0, 8).map((row) => (
              <li key={row.key}>
                <FilingLink row={row} current={pathname === `/documents/${row.documentId}`} />
              </li>
            ))}
          </ul>
        </div>
      )}

      {usage && !offline && <QuotaCompact usage={usage} />}

      <div className="ml-auto hidden lg:mt-auto lg:ml-0 lg:block">
        {offline ? (
          <p className="hidden px-2 text-xs leading-relaxed text-ink-2 lg:block">
            <span aria-hidden className="mr-1.5 inline-block size-[7px] rounded-full bg-bad" />
            Analysis service offline
          </p>
        ) : (
          usage && <Quota usage={usage} />
        )}
        <a
          href={REPO}
          className="hidden px-2 pt-3 text-xs text-ink-2 hover:text-ink lg:block"
        >
          Source on GitHub
        </a>
      </div>
    </aside>
  );
}

function NavLink({
  href,
  current,
  children,
}: {
  href: string;
  current: boolean;
  children: React.ReactNode;
}) {
  return (
    <Link
      href={href}
      aria-current={current ? "page" : undefined}
      className={`flex items-center justify-between gap-3 rounded-md px-2 py-1.5 ${
        current
          ? "bg-sheet text-ink shadow-[0_0_0_1px_var(--color-line)]"
          : "text-ink-2 hover:bg-line/60 hover:text-ink"
      }`}
    >
      {children}
    </Link>
  );
}

function FilingLink({ row, current }: { row: Row; current: boolean }) {
  const label = (
    <>
      <StatusDot status={row.status} />
      <span className="min-w-0 flex-1 truncate">
        {row.company} <span className="text-ink-2">{row.fiscalYear}</span>
      </span>
      {SHORT_STATUS[row.status.kind] && (
        <span className="text-2xs text-ink-2">{SHORT_STATUS[row.status.kind]}</span>
      )}
    </>
  );
  const base = "flex items-center gap-2 rounded-md px-2 py-1.5";
  if (row.documentId === null || row.status.kind === "empty") {
    return (
      <Link href="/" className={`${base} text-ink-2 hover:bg-line/60`}>
        {label}
      </Link>
    );
  }
  return (
    <Link
      href={`/documents/${row.documentId}`}
      aria-current={current ? "page" : undefined}
      className={`${base} ${
        current
          ? "bg-sheet text-ink shadow-[0_0_0_1px_var(--color-line)]"
          : "text-ink hover:bg-line/60"
      }`}
    >
      {label}
    </Link>
  );
}

// Narrow screens: the quota as one line in the top strip.
function QuotaCompact({ usage }: { usage: Usage }) {
  const share = Math.min(1, usage.tokens / Math.max(1, usage.token_limit));
  return (
    <p className="ml-auto flex items-center gap-2 text-xs text-ink-2 lg:hidden" title={usage.model}>
      <span className="h-1 w-10 overflow-hidden rounded-full bg-line-strong/70" aria-hidden>
        <span className="block h-full bg-ink" style={{ width: `${share * 100}%` }} />
      </span>
      <span className="tabular-nums">{Math.round(share * 100)}% of today&apos;s quota</span>
    </p>
  );
}

function Quota({ usage }: { usage: Usage }) {
  const share = Math.min(1, usage.tokens / Math.max(1, usage.token_limit));
  const model = usage.model.replace(/^[^/]+\//, "");
  return (
    <div className="hidden border-t border-line px-2 pt-3 lg:block">
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className="text-ink-2">{model} today</span>
        <span className="font-mono text-2xs text-ink-2 tabular-nums">{Math.round(share * 100)}%</span>
      </div>
      <div
        role="meter"
        aria-label={`${model} tokens used today`}
        aria-valuemin={0}
        aria-valuemax={usage.token_limit}
        aria-valuenow={usage.tokens}
        className="mt-1.5 h-1 overflow-hidden rounded-full bg-line-strong/70"
      >
        <span
          className={`block h-full rounded-full transition-[width] duration-300 ${share > 0.9 ? "bg-warn" : "bg-ink"}`}
          style={{ width: `${share * 100}%` }}
        />
      </div>
      <p className="mt-1.5 text-xs leading-relaxed text-ink-2" title={usage.model}>
        <span className="tabular-nums">
          {usage.tokens.toLocaleString("en-US")} of {plural(usage.token_limit, "token")}
        </span>
        {usage.resets_at && (
          <>
            <br />
            Resets <Clock iso={usage.resets_at} />
          </>
        )}
      </p>
    </div>
  );
}
