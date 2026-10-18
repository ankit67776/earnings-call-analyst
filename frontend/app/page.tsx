import Link from "next/link";

import AnalyzeForm from "./analyze-form";
import { api } from "./lib/api";
import { CLAIM_LABELS, companyName, plural, sectionName } from "./lib/format";
import type { DocumentSummary, Finding } from "./lib/types";

export default async function Home() {
  const documents = await api.documents();
  const example = documents.length > 0 ? await exampleQuote(documents[0]) : null;

  return (
    <main className="mx-auto max-w-3xl px-6 py-14 sm:py-20">
      <h1 className="text-4xl font-semibold tracking-tight sm:text-5xl">Earnings Call Analyst</h1>
      <p className="mt-4 max-w-[60ch] text-lg leading-relaxed text-ink-soft">
        Reads a company&apos;s annual report, pulls out what it claims about results, plans
        and risks, and shows the exact sentence in the filing behind every claim. Claims
        whose quote can&apos;t be found in the filing are dropped.
      </p>

      {example && <Example {...example} />}

      <section aria-labelledby="filings" className="mt-14">
        <h2 id="filings" className="text-xl font-semibold">
          Analyzed filings
        </h2>
        {documents.length === 0 ? (
          <p className="mt-3 text-ink-soft">
            Nothing analyzed yet. Queue a filing below; Hershey (HSY) for 2020 is a good first
            one.
          </p>
        ) : (
          <ul className="mt-4 divide-y divide-rule border-y border-rule">
            {documents.map((d) => (
              <li key={d.id}>
                <Link
                  href={`/documents/${d.id}`}
                  className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 px-1 py-4 hover:bg-page/60"
                >
                  <span>
                    <span className="font-medium">{companyName(d.company)}</span>{" "}
                    <span className="text-ink-soft">
                      {d.form} for fiscal {d.fiscal_year}
                    </span>
                  </span>
                  <span className="text-sm text-ink-soft">
                    {d.analyzed_sections.length > 0
                      ? `${plural(d.findings, "claim")} from ${d.analyzed_sections
                          .map((s) => sectionName(s).toLowerCase())
                          .join(" and ")}`
                      : "Not analyzed yet"}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="analyze" className="mt-14">
        <h2 id="analyze" className="text-xl font-semibold">
          Analyze a filing
        </h2>
        <p className="mt-2 max-w-[60ch] text-ink-soft">
          Fetches the 10-K from SEC EDGAR and extracts claims from its risk factors and
          management&apos;s discussion. On the free model tier a full filing takes 10 to 20
          minutes.
        </p>
        <AnalyzeForm />
      </section>
    </main>
  );
}

interface ExampleQuote {
  company: string;
  fiscalYear: number;
  finding: Finding;
  before: string;
  after: string;
  quoted: string;
}

// One real claim from the latest data, shown the way the filing view shows it.
async function exampleQuote(document: DocumentSummary): Promise<ExampleQuote | null> {
  const findings = await api.findings(document.id);
  const finding = findings.find(
    (f) => f.verified && f.type === "metric" && f.citations[0]?.match === "exact",
  );
  const citation = finding?.citations[0];
  if (!finding || !citation || citation.start === null || citation.end === null) return null;
  const section = await api.section(document.id, finding.section);
  const paragraph = section.paragraphs.find((p) => p.id === citation.paragraph_id);
  if (!paragraph) return null;

  // A sentence or so of context on each side of the quote.
  const from = paragraph.text.lastIndexOf(". ", Math.max(0, citation.start - 2)) + 2;
  const to = paragraph.text.indexOf(". ", citation.end);
  return {
    company: companyName(document.company),
    fiscalYear: document.fiscal_year,
    finding,
    before: (from > 1 ? "…" : "") + paragraph.text.slice(from > 1 ? from : 0, citation.start),
    quoted: paragraph.text.slice(citation.start, citation.end),
    after: paragraph.text.slice(citation.end, to === -1 ? undefined : to + 1),
  };
}

function Example({ company, fiscalYear, finding, before, quoted, after }: ExampleQuote) {
  return (
    <figure className="mt-10 rounded-sm bg-page px-6 py-6 shadow-sheet sm:px-8">
      <figcaption className="text-sm text-ink-soft">
        {CLAIM_LABELS[finding.type]} claim from {company}&apos;s {fiscalYear} 10-K
      </figcaption>
      <p className="mt-2 text-lg font-medium leading-snug">{finding.claim}</p>
      <blockquote className="mt-4 border-l-2 border-ink pl-4 font-serif leading-[1.8]">
        {before}
        <mark className="hl" data-type={finding.type} data-active="">
          {quoted}
        </mark>
        {after}
      </blockquote>
    </figure>
  );
}
