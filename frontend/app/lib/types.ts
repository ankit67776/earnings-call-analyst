// Shapes returned by the FastAPI backend (backend/app/queries.py and main.py).

export type ClaimType = "guidance" | "metric" | "risk" | "event";
export type MatchKind = "exact" | "normalized" | "elided" | "fuzzy";

export interface Citation {
  paragraph_id: string;
  match: MatchKind | null;
  quote: string;
  start: number | null;
  end: number | null;
  source_text: string | null;
}

export interface Finding {
  id: number;
  section: string;
  type: ClaimType;
  claim: string;
  verified: boolean;
  citations: Citation[];
}

export interface Paragraph {
  id: string;
  position: number;
  kind: "text" | "heading" | "table";
  text: string;
}

export interface Section {
  item: string;
  title: string;
  paragraphs: Paragraph[];
}

export interface Run {
  id: number;
  section: string;
  model: string;
  prompt_version: number;
  chunks: number;
  llm_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  seconds: number;
  created_at: string;
  findings: number;
  rejected: number;
}

export interface DocumentDetail {
  id: number;
  ticker: string;
  company: string;
  form: string;
  fiscal_year: number;
  accession: string;
  period: string;
  filed: string;
  url: string;
  sections: { item: string; title: string; paragraphs: number }[];
  runs: Run[];
}

export interface DocumentSummary {
  id: number;
  ticker: string;
  company: string;
  form: string;
  fiscal_year: number;
  filed: string;
  analyzed_sections: string[];
  findings: number;
  // Claims dropped because their quote wasn't found in the filing.
  rejected: number;
}

// Written by the worker as it goes (backend/app/jobs.py, analyze_filing).
export interface JobProgress {
  step: "fetching" | "extracting";
  sections: string[];
  done: string[];
  section?: string;
  chunks_done?: number;
  chunks?: number;
  started_at?: string;
}

export interface Job {
  id: number;
  kind: string;
  status: "queued" | "running" | "done" | "failed";
  attempts: number;
  max_attempts: number;
  payload: { ticker: string; fiscal_year: number; sections?: string[] };
  result: { document_id: number; runs: Record<string, number> } | null;
  error: string | null;
  progress: JobProgress | null;
  run_after: string;
  created_at: string;
  updated_at: string;
}

export interface Usage {
  model: string;
  tokens: number;
  requests: number;
  token_limit: number;
  request_limit: number;
  resets_at: string | null;
}
