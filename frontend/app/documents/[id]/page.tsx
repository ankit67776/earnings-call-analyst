import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { NotFoundError, api } from "@/app/lib/api";
import { companyName } from "@/app/lib/format";
import type { Section } from "@/app/lib/types";
import Workspace from "./workspace";

async function load(id: number) {
  try {
    const [document, findings] = await Promise.all([api.document(id), api.findings(id)]);
    const analyzed = document.runs.map((run) => run.section);
    const sections: Section[] = await Promise.all(analyzed.map((item) => api.section(id, item)));
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

export default async function DocumentPage({ params }: PageProps<"/documents/[id]">) {
  const { document, findings, sections } = await load(documentId((await params).id));
  return <Workspace document={document} findings={findings} sections={sections} />;
}
