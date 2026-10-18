import type { Metadata } from "next";

import { api } from "@/app/lib/api";
import type { Company } from "@/app/lib/types";
import Ask from "./ask";

export const metadata: Metadata = { title: "Ask" };

export default async function AskPage({ searchParams }: PageProps<"/ask">) {
  let companies: Company[] | null = null;
  try {
    companies = (await api.companies()).filter((c) => c.passages > 0);
  } catch {
    companies = null;
  }
  const { ticker, q } = await searchParams;
  return (
    <Ask
      companies={companies}
      initialTicker={typeof ticker === "string" ? ticker.toUpperCase() : null}
      // A link can carry a question (?ticker=HSY&q=...), asked on arrival.
      initialQuestion={typeof q === "string" ? q.slice(0, 500) : null}
    />
  );
}
