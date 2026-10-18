"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useState } from "react";

import type { Job } from "./lib/types";

const POLL_MS = 4000;

function clock(iso: string): string {
  return new Date(iso).toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" });
}

function describe(job: Job): string {
  switch (job.status) {
    case "queued":
      if (new Date(job.run_after) > new Date()) {
        return job.error
          ? `Waiting to try again (${job.error}). Next attempt ${clock(job.run_after)}.`
          : `Scheduled for ${clock(job.run_after)}.`;
      }
      return "Queued. A worker will pick it up shortly.";
    case "running":
      return "Reading the filing and checking every quote. This updates on its own.";
    case "done":
      return "Analysis finished.";
    case "failed":
      return `The analysis failed: ${job.error ?? "no error message"}`;
  }
}

export default function AnalyzeForm() {
  const router = useRouter();
  const [ticker, setTicker] = useState("");
  const [year, setYear] = useState("2020");
  const [jobId, setJobId] = useState<number | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (jobId === null) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;

    async function poll() {
      try {
        const response = await fetch(`/api/jobs/${jobId}`, { cache: "no-store" });
        if (!response.ok) throw new Error(String(response.status));
        const next = (await response.json()) as Job;
        if (stopped) return;
        setJob(next);
        setProblem(null);
        if (next.status === "done") {
          router.refresh(); // the new filing shows up in the list
          return;
        }
        if (next.status === "failed") return;
      } catch {
        if (!stopped) setProblem("Lost contact with the analysis service; still trying.");
      }
      if (!stopped) timer = setTimeout(poll, POLL_MS);
    }

    timer = setTimeout(poll, 0);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [jobId, router]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setProblem(null);
    setJob(null);
    setJobId(null);
    setSubmitting(true);
    try {
      const response = await fetch("/api/analyses", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ticker: ticker.trim(), fiscal_year: Number(year) }),
      });
      if (response.status === 422) {
        setProblem("Check the ticker (letters only, like HSY) and the fiscal year.");
      } else if (!response.ok) {
        setProblem(`The analysis service returned an error (${response.status}).`);
      } else {
        const { job_id } = (await response.json()) as { job_id: number };
        setJobId(job_id);
      }
    } catch {
      setProblem("Couldn't reach the analysis service. Start the backend and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  const busy = submitting || job?.status === "queued" || job?.status === "running";

  return (
    <div className="mt-5">
      <form onSubmit={submit} className="flex flex-wrap items-end gap-4">
        <label className="flex flex-col gap-1.5 text-sm">
          Ticker
          <input
            required
            value={ticker}
            onChange={(e) => setTicker(e.target.value.toUpperCase())}
            pattern="[A-Za-z][A-Za-z.\-]{0,9}"
            placeholder="HSY"
            autoComplete="off"
            className="w-28 rounded-md border border-ink/25 bg-page px-3 py-2 text-base uppercase"
          />
        </label>
        <label className="flex flex-col gap-1.5 text-sm">
          Fiscal year
          <input
            required
            type="number"
            min={1994}
            max={2100}
            value={year}
            onChange={(e) => setYear(e.target.value)}
            className="w-28 rounded-md border border-ink/25 bg-page px-3 py-2 text-base tabular-nums"
          />
        </label>
        <button
          type="submit"
          disabled={busy}
          className="rounded-md bg-ink px-4 py-2 font-medium text-page hover:bg-ink/85 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Analyze filing
        </button>
      </form>

      <div role="status" aria-live="polite" className="mt-4 min-h-6 max-w-[65ch]">
        {job && (
          <p>
            {job.payload.ticker} {job.payload.fiscal_year}: {describe(job)}{" "}
            {job.status === "done" && job.result && (
              <Link
                href={`/documents/${job.result.document_id}`}
                className="font-medium underline underline-offset-4"
              >
                Open the analysis
              </Link>
            )}
          </p>
        )}
        {problem && <p className="text-ink-soft">{problem}</p>}
      </div>
    </div>
  );
}
