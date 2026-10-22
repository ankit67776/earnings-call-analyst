import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { NotFoundError, api } from "@/app/lib/api";
import { companyName } from "@/app/lib/format";
import LiveCall from "./live-call";

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

export async function generateMetadata({ params }: PageProps<"/calls/[id]/live">): Promise<Metadata> {
  const call = await load((await params).id);
  return { title: `${companyName(call.company)} Q${call.quarter} ${call.fiscal_year} call, live` };
}

export default async function LivePage({ params }: PageProps<"/calls/[id]/live">) {
  const call = await load((await params).id);
  return <LiveCall call={call} />;
}
