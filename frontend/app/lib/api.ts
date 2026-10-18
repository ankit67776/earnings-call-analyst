import type {
  CallDetail,
  Company,
  DocumentDetail,
  DocumentSummary,
  Finding,
  Job,
  Section,
  Usage,
} from "./types";

// Server Components call the backend directly; there's no Next server to proxy through
// at build time. The browser uses the /api rewrite in next.config.ts instead.
const BACKEND = process.env.BACKEND_URL ?? "http://localhost:8000";

export class NotFoundError extends Error {}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`${BACKEND}${path}`, { cache: "no-store" });
  if (response.status === 404) throw new NotFoundError(path);
  if (!response.ok) throw new Error(`${path}: the backend returned ${response.status}`);
  return (await response.json()) as T;
}

export const api = {
  documents: () => get<DocumentSummary[]>("/api/documents"),
  document: (id: number) => get<DocumentDetail>(`/api/documents/${id}`),
  findings: (id: number) => get<Finding[]>(`/api/documents/${id}/findings`),
  section: (id: number, item: string) =>
    get<Section>(`/api/documents/${id}/sections/${encodeURIComponent(item)}`),
  jobs: () => get<Job[]>("/api/jobs"),
  usage: () => get<Usage>("/api/usage"),
  companies: () => get<Company[]>("/api/companies"),
  call: (id: number) => get<CallDetail>(`/api/calls/${id}`),
};
