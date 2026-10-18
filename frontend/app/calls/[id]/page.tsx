import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { NotFoundError, api } from "@/app/lib/api";
import { companyName } from "@/app/lib/format";
import Transcript from "./transcript";

async function load(raw: string) {
  const id = Number(raw);
  if (!Number.isInteger(id) || id <= 0) notFound();
  try {
    return await api.call(id);
  } catch (error) {
    if (error instanceof NotFoundError) notFound();
    throw error;
  }
}

export async function generateMetadata({ params }: PageProps<"/calls/[id]">): Promise<Metadata> {
  const call = await load((await params).id);
  return { title: `${companyName(call.company)} Q${call.quarter} ${call.fiscal_year} call` };
}

export default async function CallPage({ params, searchParams }: PageProps<"/calls/[id]">) {
  const call = await load((await params).id);
  const { t } = await searchParams;
  const at = typeof t === "string" && /^\d+(\.\d+)?$/.test(t) ? Number(t) : null;
  return <Transcript key={at ?? "top"} call={call} at={at} />;
}
