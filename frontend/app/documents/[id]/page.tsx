import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { NotFoundError, api } from "@/app/lib/api";
import { companyName } from "@/app/lib/format";
import type { Section } from "@/app/lib/types";
import Reader from "./reader";

async function load(id: number, extra: string | null = null) {
  try {
    const [document, findings] = await Promise.all([api.document(id), api.findings(id)]);
    const shown = document.runs.map((run) => run.section);
    // A section asked for by link (a chat citation) is shown even if it wasn't analyzed.
    if (extra && !shown.includes(extra) && document.sections.some((s) => s.item === extra)) {
      shown.push(extra);
    }
    const sections: Section[] = await Promise.all(shown.map((item) => api.section(id, item)));
    return { document, findings, sections };
  } catch (error) {
    if (error instanceof NotFoundError) notFound();
    throw error;
  }
}

function documentId(raw: string): number {
  const id = Number(raw);
  if (!Number.isInteger(id) || id <= 0) notFound();
  return id;
}

export async function generateMetadata({ params }: PageProps<"/documents/[id]">): Promise<Metadata> {
  const { document } = await load(documentId((await params).id));
  return { title: `${companyName(document.company)} ${document.form} ${document.fiscal_year}` };
}

export default async function DocumentPage({ params, searchParams }: PageProps<"/documents/[id]">) {
  const { claim, item, p } = await searchParams;
  const requested = typeof item === "string" ? item.toUpperCase() : null;
  const { document, findings, sections } = await load(documentId((await params).id), requested);
  return (
    <Reader
      // A different claim, section or paragraph in the URL starts the view over.
      key={`${claim}-${item}-${p}`}
      document={document}
      findings={findings}
      sections={sections}
      initialClaim={typeof claim === "string" && /^\d+$/.test(claim) ? Number(claim) : null}
      initialItem={requested}
      initialParagraph={typeof p === "string" && /^[\w-]{1,40}$/.test(p) ? p : null}
    />
  );
}
