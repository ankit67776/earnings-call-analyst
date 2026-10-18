import { companyName, sectionShort } from "./format";
import type { DocumentSummary, Job } from "./types";

// What a library row says about a filing: finished, in progress, waiting or failed.
export type Status =
  | { kind: "ready" }
  | { kind: "empty" }
  | { kind: "queued" }
  | { kind: "fetching" }
  | {
      kind: "extracting";
      section: string;
      chunksDone: number;
      chunks: number;
      minutesLeft: number | null;
      next: string[];
    }
  | { kind: "waiting"; until: string; quota: boolean; error: string | null }
  // `detail` is the raw error, for a tooltip.
  | { kind: "failed"; error: string; detail: string | null };

export interface Row {
  key: string;
  documentId: number | null;
  ticker: string;
  company: string;
  form: string;
  fiscalYear: number;
  filed: string | null;
  sections: string[];
  claims: number | null;
  rejected: number | null;
  status: Status;
  job: Job | null;
}

const QUOTA = /budget|rate limit/i;

export function isActive(job: Job, now = Date.now()): boolean {
  return job.status === "running" || (job.status === "queued" && Date.parse(job.run_after) <= now);
}

// What a failed job's error means for the person who queued it.
export function failure(job: Job): string {
  const { ticker, fiscal_year } = job.payload;
  const error = job.error ?? "";
  if (error.startsWith("FilingNotFoundError")) {
    return `No 10-K for ${ticker} in fiscal ${fiscal_year} on SEC EDGAR. Check the ticker or pick another year.`;
  }
  return `Stopped after ${job.attempts} ${job.attempts === 1 ? "try" : "tries"}. Queue it again to retry.`;
}

export function jobStatus(job: Job, now = Date.now()): Status {
  if (job.status === "failed") return { kind: "failed", error: failure(job), detail: job.error };
  if (job.status === "done") return { kind: "ready" };
  if (job.status === "queued") {
    if (Date.parse(job.run_after) > now + 5_000) {
      return {
        kind: "waiting",
        until: job.run_after,
        quota: QUOTA.test(job.error ?? ""),
        error: job.error,
      };
    }
    return { kind: "queued" };
  }
  const p = job.progress;
  if (p?.step !== "extracting" || !p.section || p.chunks === undefined) return { kind: "fetching" };
  const done = p.chunks_done ?? 0;
  let minutesLeft: number | null = null;
  if (p.started_at && done >= 2 && done < p.chunks) {
    const perChunk = (now - Date.parse(p.started_at)) / done;
    minutesLeft = Math.max(1, Math.round((perChunk * (p.chunks - done)) / 60_000));
  }
  const next = p.sections.filter((s) => s !== p.section && !p.done.includes(s));
  return { kind: "extracting", section: p.section, chunksDone: done, chunks: p.chunks, minutesLeft, next };
}

function key(ticker: string, year: number): string {
  return `${ticker.toUpperCase()}-${year}`;
}

/**
 * One row per filing: analyzed documents, plus jobs that haven't produced a document
 * yet. A filing's latest job decides its status while it's running, waiting or failed.
 */
export function libraryRows(documents: DocumentSummary[], jobs: Job[], now = Date.now()): Row[] {
  const latest = new Map<string, Job>();
  for (const job of jobs) {
    if (job.kind !== "analyze_filing") continue;
    const k = key(job.payload.ticker, job.payload.fiscal_year);
    const seen = latest.get(k);
    if (!seen || seen.id < job.id) latest.set(k, job);
  }

  const rows: Row[] = documents.map((d) => {
    const job = latest.get(key(d.ticker, d.fiscal_year)) ?? null;
    latest.delete(key(d.ticker, d.fiscal_year));
    const analyzed = d.analyzed_sections.length > 0;
    const status: Status =
      job && job.status !== "done" ? jobStatus(job, now) : analyzed ? { kind: "ready" } : { kind: "empty" };
    return {
      key: key(d.ticker, d.fiscal_year),
      documentId: d.id,
      ticker: d.ticker,
      company: companyName(d.company),
      form: d.form,
      fiscalYear: d.fiscal_year,
      filed: d.filed,
      sections: d.analyzed_sections,
      claims: analyzed ? d.findings : null,
      rejected: analyzed ? d.rejected : null,
      status,
      job,
    };
  });

  const pending: Row[] = [...latest.values()]
    .filter((job) => job.status !== "done")
    .map((job) => ({
      key: key(job.payload.ticker, job.payload.fiscal_year),
      documentId: job.result?.document_id ?? null,
      ticker: job.payload.ticker,
      company: job.payload.ticker,
      form: "10-K",
      fiscalYear: job.payload.fiscal_year,
      filed: null,
      sections: [],
      claims: null,
      rejected: null,
      status: jobStatus(job, now),
      job,
    }));

  return [...pending, ...rows];
}

export function sectionList(items: string[]): string {
  return items.map((s) => sectionShort(s)).join(", ");
}
