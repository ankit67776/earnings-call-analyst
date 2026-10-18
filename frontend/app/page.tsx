import AnalyzeForm from "./analyze-form";
import Library, { ExampleLink } from "./library";

export default function Home() {
  return (
    <>
      <main className="mx-auto max-w-[76rem] px-4 pt-8 pb-16 sm:px-8">
        <h1 className="text-xl font-semibold tracking-[-0.015em]">Library</h1>
        <p className="mt-1 max-w-[70ch] leading-relaxed text-ink-2">
          10-K filings read by the model. Every claim it pulls out links to the sentence it
          quotes, and claims whose quote can&apos;t be found in the filing are dropped.
        </p>
        <ExampleLink />
        <AnalyzeForm />
        <Library />
        <HowItWorks />
      </main>
    </>
  );
}

const STEPS = [
  ["Fetch", "The 10-K comes straight from SEC EDGAR."],
  ["Split", "Risk factors and management's discussion are cut into numbered paragraphs."],
  ["Extract", "gpt-oss-120b lists the claims, each with a quote copied from a paragraph."],
  ["Check", "Every quote is searched for in the filing. Claims whose quote isn't there are dropped."],
] as const;

function HowItWorks() {
  return (
    <section aria-labelledby="how" className="mt-12 border-t border-line pt-5">
      <h2 id="how" className="font-medium">
        How each filing is checked
      </h2>
      <ol className="mt-3 grid gap-x-8 gap-y-3 text-ink-2 sm:grid-cols-2 xl:grid-cols-4">
        {STEPS.map(([name, text], i) => (
          <li key={name} className="flex gap-3 leading-relaxed">
            <span className="font-mono text-xs leading-[1.4rem] text-ink-3 tabular-nums">{i + 1}</span>
            <span>
              <span className="font-medium text-ink">{name}.</span> {text}
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}
