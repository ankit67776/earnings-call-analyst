"use client";

import { type FormEvent, useEffect, useRef, useState } from "react";

import { useLive } from "./lib/live";
import type { Job } from "./lib/types";

// 10-Ks for a fiscal year are filed early the next year.
const LATEST_YEAR = new Date().getFullYear() - 1;
const YEARS = Array.from({ length: 16 }, (_, i) => LATEST_YEAR - i);

export default function AnalyzeForm() {
  const { track } = useLive();
  const [ticker, setTicker] = useState("");
  const [year, setYear] = useState(String(LATEST_YEAR));
  const [problem, setProblem] = useState<string | null>(null);
  const [queued, setQueued] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  // "Analyze a filing" elsewhere in the app links to /#analyze.
  useEffect(() => {
    function focusOnHash() {
      if (window.location.hash === "#analyze") input.current?.focus();
    }
    focusOnHash();
    window.addEventListener("hashchange", focusOnHash);
    return () => window.removeEventListener("hashchange", focusOnHash);
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setProblem(null);
    setQueued(null);
    setSubmitting(true);
    const symbol = ticker.trim().toUpperCase();
    try {
      const response = await fetch("/api/analyses", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ticker: symbol, fiscal_year: Number(year) }),
      });
      if (response.status === 422) {
        setProblem("Use a ticker of letters only, like HSY.");
      } else if (!response.ok) {
        setProblem(`The analysis service returned an error (${response.status}). Try again.`);
      } else {
        const { job_id } = (await response.json()) as { job_id: number };
        const job = await fetch(`/api/jobs/${job_id}`, { cache: "no-store" });
        if (job.ok) track((await job.json()) as Job);
        setQueued(`${symbol} ${year} is queued. It shows up in the table below as it runs.`);
        setTicker("");
      }
    } catch {
      setProblem("Couldn't reach the analysis service. Start the backend and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section aria-labelledby="analyze-title" className="mt-6">
      <form
        onSubmit={submit}
        className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-line bg-sheet px-3 py-2.5"
      >
        <h2 id="analyze-title" className="pr-2 pl-1 font-medium">
          Analyze a 10-K
        </h2>
        <label className="sr-only" htmlFor="analyze">
          Ticker
        </label>
        <input
          id="analyze"
          ref={input}
          required
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          pattern="[A-Za-z][A-Za-z.\-]{0,9}"
          placeholder="Ticker, e.g. HSY"
          autoComplete="off"
          spellCheck={false}
          aria-describedby="analyze-note"
          className="h-8 w-40 rounded-md border border-line-strong bg-sheet px-2.5 font-mono text-[0.8125rem] uppercase placeholder:font-sans placeholder:normal-case placeholder:text-ink-3 hover:border-ink-3"
        />
        <label className="sr-only" htmlFor="analyze-year">
          Fiscal year
        </label>
        <select
          id="analyze-year"
          value={year}
          onChange={(e) => setYear(e.target.value)}
          className="h-8 rounded-md border border-line-strong bg-sheet px-2 tabular-nums hover:border-ink-3"
        >
          {YEARS.map((y) => (
            <option key={y} value={y}>
              Fiscal {y}
            </option>
          ))}
        </select>
        <button
          type="submit"
          disabled={submitting}
          className="h-8 rounded-md bg-ink px-3 font-medium text-sheet hover:bg-ink/85 disabled:cursor-progress disabled:opacity-60"
        >
          {submitting ? "Queuing…" : "Analyze"}
        </button>
        <p id="analyze-note" className="text-ink-2 sm:ml-2">
          Reads risk factors and MD&amp;A. Takes 10 to 20 minutes and keeps going if you
          leave.
        </p>
      </form>
      <p role="status" aria-live="polite" className="min-h-5 px-1 pt-1.5 text-ink-2">
        {problem ? <span className="text-bad">{problem}</span> : queued}
      </p>
    </section>
  );
}
