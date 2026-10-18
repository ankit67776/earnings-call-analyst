import type { Metadata } from "next";
import { Geist, Geist_Mono, Source_Serif_4 } from "next/font/google";

import { api } from "./lib/api";
import { LiveProvider, type LiveData } from "./lib/live";
import Sidebar from "./sidebar";
import "./globals.css";

// Geist for the interface and figures; Source Serif 4 for the filing text itself.
const sans = Geist({ variable: "--font-geist", subsets: ["latin"] });
const mono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });
const serif = Source_Serif_4({ variable: "--font-source-serif", subsets: ["latin"] });

export const metadata: Metadata = {
  title: { default: "Earnings Call Analyst", template: "%s · Earnings Call Analyst" },
  description:
    "Claims pulled from SEC 10-K filings, each shown next to the exact sentence it comes from.",
};

// The shell's filings, jobs and quota. The pages render their own error if the
// analysis service is down; the sidebar just says so.
async function initialData(): Promise<LiveData> {
  const [documents, jobs, usage, companies] = await Promise.allSettled([
    api.documents(),
    api.jobs(),
    api.usage(),
    api.companies(),
  ]);
  return {
    documents: documents.status === "fulfilled" ? documents.value : null,
    companies: companies.status === "fulfilled" ? companies.value : null,
    jobs: jobs.status === "fulfilled" ? jobs.value : [],
    usage: usage.status === "fulfilled" ? usage.value : null,
    offline: documents.status === "rejected",
  };
}

export default async function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${sans.variable} ${mono.variable} ${serif.variable} antialiased`}>
      <body className="bg-canvas font-sans text-sm text-ink">
        <LiveProvider initial={await initialData()}>
          <div className="lg:grid lg:h-dvh lg:grid-cols-[14rem_minmax(0,1fr)]">
            <Sidebar />
            <div id="main" className="min-w-0 lg:overflow-y-auto">
              {children}
            </div>
          </div>
        </LiveProvider>
      </body>
    </html>
  );
}
