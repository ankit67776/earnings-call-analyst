"use client";

import Link from "next/link";

import { sectionShort } from "./lib/format";
import { useLive } from "./lib/live";
import { type Row, type Status, libraryRows, sectionList } from "./lib/status";
import { Clock } from "./lib/time";
import { StatusDot } from "./sidebar";

function Chevron() {
  return (
    <svg viewBox="0 0 16 16" aria-hidden className="size-3.5 shrink-0">
      <path
        d="m6 4 4 4-4 4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// For a first visit: the quickest way to see what a finished analysis looks like.
export function ExampleLink() {
  const { documents, jobs } = useLive();
  const ready = libraryRows(documents ?? [], jobs).find(
    (r) => r.status.kind === "ready" && r.documentId !== null,
  );
  if (!ready) return null;
  return (
    <Link
      href={`/documents/${ready.documentId}`}
      className="mt-3 inline-flex items-center gap-1 font-medium underline decoration-line-strong underline-offset-4 hover:decoration-ink"
    >
      New here? Open {ready.company}&apos;s {ready.fiscalYear} 10-K
      <Chevron />
    </Link>
  );
}

export default function Library() {
  const { documents, jobs, offline } = useLive();

  if (documents === null) {
    return (
      <div className="mt-2 rounded-lg border border-line bg-sheet px-5 py-6">
        <p className="font-medium">Can&apos;t reach the analysis service</p>
        <p className="mt-1 max-w-[65ch] leading-relaxed text-ink-2">
          The library comes from the FastAPI backend. Start it with{" "}
          <code className="font-mono text-[0.8125rem]">uv run uvicorn app.main:app</code> in the{" "}
          <code className="font-mono text-[0.8125rem]">backend</code> folder; this page
          reconnects on its own.
        </p>
      </div>
    );
  }

  const rows = libraryRows(documents, jobs);
  if (rows.length === 0) {
    return (
      <p className="mt-2 rounded-lg border border-dashed border-line-strong px-5 py-6 text-ink-2">
        No filings yet. Analyze one above; Hershey (HSY) for fiscal 2020 is a good first
        one.
      </p>
    );
  }

  return (
    <div className="mt-2 overflow-x-auto rounded-lg border border-line bg-sheet">
      <table className="w-full border-collapse text-left">
        <thead>
          <tr className="border-b border-line text-xs text-ink-2">
            <th scope="col" className="py-2 pr-3 pl-4 font-medium">
              Company
            </th>
            <th scope="col" className="hidden px-3 py-2 font-medium sm:table-cell">
              Filing
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Status
            </th>
            <th scope="col" className="hidden px-3 py-2 text-right font-medium md:table-cell">
              Claims
            </th>
            <th scope="col" className="py-2 pr-4 pl-3 text-right font-medium md:pr-3">
              Quotes found in filing
            </th>
            <th scope="col" className="hidden py-2 pr-4 pl-3 text-right font-medium lg:table-cell">
              Filed
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <LibraryRow key={row.key} row={row} />
          ))}
        </tbody>
      </table>
      {offline && (
        <p className="border-t border-line px-4 py-2 text-xs text-ink-2">
          Lost contact with the analysis service; showing the last known state.
        </p>
      )}
    </div>
  );
}

function LibraryRow({ row }: { row: Row }) {
  const openable = row.documentId !== null && row.claims !== null;
  const total = (row.claims ?? 0) + (row.rejected ?? 0);
  return (
    <tr
      className={`relative border-b border-line last:border-0 ${openable ? "hover:bg-canvas" : ""}`}
    >
      <td className="py-3 pr-3 pl-4 align-top">
        {openable ? (
          // Stretched over the row so the whole row opens the filing.
          <Link
            href={`/documents/${row.documentId}`}
            className="inline-flex items-center gap-0.5 font-medium whitespace-nowrap underline decoration-line-strong underline-offset-4 after:absolute after:inset-0 after:content-[''] hover:decoration-ink"
          >
            {row.company}
            <Chevron />
          </Link>
        ) : (
          <span className="font-medium">{row.company}</span>
        )}
        {/* Until the filing is fetched, the ticker is all there is to show. */}
        {row.company !== row.ticker && (
          <span className="ml-2 font-mono text-xs text-ink-2">{row.ticker}</span>
        )}
        <div className="text-xs text-ink-2 sm:hidden">
          {row.form}, fiscal {row.fiscalYear}
        </div>
      </td>
      <td className="hidden px-3 py-3 align-top whitespace-nowrap text-ink-2 sm:table-cell">
        {row.form}, fiscal <span className="tabular-nums">{row.fiscalYear}</span>
      </td>
      <td className="px-3 py-3 align-top">
        <StatusCell status={row.status} sections={row.sections} rejected={row.rejected} />
      </td>
      <td className="hidden px-3 py-3 text-right align-top tabular-nums md:table-cell">
        {row.claims === null ? <Dash /> : row.claims.toLocaleString("en-US")}
      </td>
      <td className="py-3 pr-4 pl-3 text-right align-top whitespace-nowrap tabular-nums md:pr-3">
        {row.claims === null ? (
          <Dash />
        ) : (
          <span className={row.rejected ? "" : "text-ok"}>
            {row.claims.toLocaleString("en-US")} of {total.toLocaleString("en-US")}
          </span>
        )}
      </td>
      <td className="hidden py-3 pr-4 pl-3 text-right align-top whitespace-nowrap text-ink-2 tabular-nums lg:table-cell">
        {row.filed ? formatDate(row.filed) : <Dash />}
      </td>
    </tr>
  );
}

function StatusCell({
  status,
  sections,
  rejected,
}: {
  status: Status;
  sections: string[];
  rejected: number | null;
}) {
  let title: string;
  let detail: React.ReactNode = null;
  switch (status.kind) {
    case "ready":
      title = rejected ? `Analyzed, ${rejected} dropped` : "Analyzed, every quote found";
      detail = sectionList(sections);
      break;
    case "empty":
      title = "Not analyzed";
      break;
    case "queued":
      title = "Queued";
      detail = "Starts in a few seconds";
      break;
    case "fetching":
      title = "Fetching the filing";
      detail = "From SEC EDGAR";
      break;
    case "extracting":
      title = "Extracting claims";
      detail = (
        <span suppressHydrationWarning>
          {sectionShort(status.section)}: {status.chunksDone} of {status.chunks} passages
          {status.minutesLeft !== null && `, about ${status.minutesLeft} min left`}
          {status.next.length > 0 && `, then ${sectionList(status.next)}`}
        </span>
      );
      break;
    case "waiting":
      title = status.quota ? "Waiting for model quota" : "Retrying after an error";
      detail = (
        <span title={status.error ?? undefined}>
          {status.quota ? "Resumes" : "Next try"} <Clock iso={status.until} />
        </span>
      );
      break;
    case "failed":
      title = "Failed";
      detail = <span title={status.detail ?? undefined}>{status.error}</span>;
      break;
  }
  return (
    <div className="flex gap-2">
      <span className="pt-[0.4rem]">
        <StatusDot status={status} />
      </span>
      <div className="min-w-0">
        <div>{title}</div>
        {detail && <div className="max-w-[40ch] text-xs leading-relaxed text-ink-2">{detail}</div>}
        {status.kind === "extracting" && (
          <div
            role="progressbar"
            aria-label="Passages read in this section"
            aria-valuemin={0}
            aria-valuemax={status.chunks}
            aria-valuenow={status.chunksDone}
            className="mt-1.5 h-1 w-36 overflow-hidden rounded-full bg-line"
          >
            <span
              className="block h-full rounded-full bg-accent transition-[width] duration-300"
              style={{ width: `${(100 * status.chunksDone) / Math.max(1, status.chunks)}%` }}
            />
          </div>
        )}
      </div>
    </div>
  );
}

function Dash() {
  return <span className="text-ink-3">–</span>;
}

function formatDate(iso: string): string {
  return new Date(`${iso}T00:00:00`).toLocaleDateString("en-US", { dateStyle: "medium" });
}
