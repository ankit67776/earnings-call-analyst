"use client";

import { useRouter } from "next/navigation";
import {
  type ReactNode,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";

import { isActive } from "./status";
import type { Company, DocumentSummary, Job, Usage } from "./types";

export interface LiveData {
  documents: DocumentSummary[] | null;
  // Loaded with the page: what the chat can search, and the calls to read.
  companies: Company[] | null;
  jobs: Job[];
  usage: Usage | null;
  // The analysis service didn't answer the last request.
  offline: boolean;
}

interface Live extends LiveData {
  refresh: () => Promise<void>;
  // Show a job the moment it's queued, before the next refresh picks it up.
  track: (job: Job) => void;
}

const LiveContext = createContext<Live | null>(null);

const ACTIVE_POLL_MS = 4_000;
const RETRY_MS = 15_000;
// The longest a deferred job's tab sleeps before checking again.
const MAX_WAIT_MS = 30 * 60_000;

async function load<T>(path: string): Promise<T> {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return (await response.json()) as T;
}

// How long until something could change: soon while a job runs, at the next deferred
// job's start time otherwise, and never when nothing is pending. Idle tabs stay quiet
// so the hosted database can scale to zero.
function nextPoll(data: LiveData, now: number): number | null {
  if (data.offline) return RETRY_MS;
  let wait: number | null = null;
  for (const job of data.jobs) {
    if (isActive(job, now)) return ACTIVE_POLL_MS;
    if (job.status === "queued") {
      const until = Math.max(ACTIVE_POLL_MS, Date.parse(job.run_after) - now + 2_000);
      wait = Math.min(wait ?? MAX_WAIT_MS, until);
    }
  }
  return wait;
}

export function LiveProvider({ initial, children }: { initial: LiveData; children: ReactNode }) {
  const router = useRouter();
  const [data, setData] = useState(initial);
  const finished = useRef(new Set(initial.jobs.filter((j) => j.status === "done").map((j) => j.id)));

  const refresh = useCallback(async () => {
    const [documents, jobs, usage] = await Promise.allSettled([
      load<DocumentSummary[]>("/api/documents"),
      load<Job[]>("/api/jobs"),
      load<Usage>("/api/usage"),
    ]);
    setData((current) => ({
      ...current,
      documents: documents.status === "fulfilled" ? documents.value : current.documents,
      jobs: jobs.status === "fulfilled" ? jobs.value : current.jobs,
      usage: usage.status === "fulfilled" ? usage.value : current.usage,
      offline: documents.status === "rejected" || jobs.status === "rejected",
    }));
    if (jobs.status !== "fulfilled") return;
    // A job just finished: re-render server pages (an open filing gains its claims).
    const newlyDone = jobs.value.filter((j) => j.status === "done" && !finished.current.has(j.id));
    for (const job of newlyDone) finished.current.add(job.id);
    if (newlyDone.length > 0) router.refresh();
  }, [router]);

  const track = useCallback((job: Job) => {
    setData((current) => ({ ...current, jobs: [job, ...current.jobs.filter((j) => j.id !== job.id)] }));
  }, []);

  useEffect(() => {
    const delay = nextPoll(data, Date.now());
    if (delay === null) return;
    const timer = setTimeout(refresh, delay);
    return () => clearTimeout(timer);
  }, [data, refresh]);

  useEffect(() => {
    function onVisible() {
      if (document.visibilityState === "visible") void refresh();
    }
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [refresh]);

  return <LiveContext value={{ ...data, refresh, track }}>{children}</LiveContext>;
}

export function useLive(): Live {
  const live = useContext(LiveContext);
  if (!live) throw new Error("useLive needs a LiveProvider");
  return live;
}
