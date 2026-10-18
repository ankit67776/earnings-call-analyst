import type { Finding, MatchKind } from "@/app/lib/types";

const KINDS: MatchKind[] = ["exact", "normalized", "elided", "fuzzy"];

// Green means found in the filing; the shade says how exactly (globals.css).
const SHADE: Record<MatchKind, string> = {
  exact: "var(--color-match-exact)",
  normalized: "var(--color-match-normalized)",
  elided: "var(--color-match-elided)",
  fuzzy: "var(--color-match-fuzzy)",
};

const LEGEND: Record<MatchKind, string> = {
  exact: "exact",
  normalized: "punctuation only",
  elided: "shortened",
  fuzzy: "close match",
};

const EXPLAIN: Record<MatchKind, string> = {
  exact: "found character for character.",
  normalized: "same words; spacing, quotes or dashes differ.",
  elided: "the model left words out, but every part was found, in order.",
  fuzzy: "small copying slips, like a changed word.",
};

function Arrow() {
  return (
    <svg viewBox="0 0 16 16" aria-hidden className="size-3.5 shrink-0 self-center text-ink-3">
      <path
        d="M3 8h9m-3.5-3.5L12 8l-3.5 3.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// The verification funnel: extracted, found in the filing, dropped, and how exactly
// the kept quotes matched.
export default function Funnel({ findings }: { findings: Finding[] }) {
  const kept = findings.filter((f) => f.verified);
  const dropped = findings.length - kept.length;
  const counts = Object.fromEntries(KINDS.map((k) => [k, 0])) as Record<MatchKind, number>;
  for (const f of kept) for (const c of f.citations) if (c.match) counts[c.match] += 1;
  const quotes = KINDS.reduce((n, k) => n + counts[k], 0);
  const shown = KINDS.filter((k) => counts[k] > 0);
  const fmt = (n: number) => n.toLocaleString("en-US");

  return (
    <section
      aria-label="How the claims were checked"
      className="flex flex-wrap items-center gap-x-8 gap-y-2.5 border-b border-line bg-sheet px-4 py-2.5 sm:px-5"
    >
      <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-ink-2">
        <span>
          <b className="font-semibold text-ink tabular-nums">{fmt(findings.length)}</b> claims
          extracted
        </span>
        <Arrow />
        <span>
          <b className="font-semibold text-ink tabular-nums">{fmt(kept.length)}</b> quotes found
          in the filing
        </span>
        <Arrow />
        <span>
          <b className={`font-semibold tabular-nums ${dropped ? "text-bad" : "text-ink"}`}>
            {fmt(dropped)}
          </b>{" "}
          dropped
        </span>
      </p>

      {quotes > 0 && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
          <div
            role="img"
            aria-label={`Quote matches: ${shown.map((k) => `${counts[k]} ${LEGEND[k]}`).join(", ")}`}
            className="flex h-1.5 w-36 gap-[2px]"
          >
            {shown.map((k) => (
              <span
                key={k}
                title={`${fmt(counts[k])} ${LEGEND[k]}`}
                className="h-full first:rounded-l-[4px] last:rounded-r-[4px]"
                style={{ flexGrow: counts[k], flexBasis: 0, minWidth: 3, background: SHADE[k] }}
              />
            ))}
          </div>
          <ul className="flex flex-wrap gap-x-3 text-xs text-ink-2">
            {shown.map((k) => (
              <li key={k} className="flex items-center gap-1.5">
                <span aria-hidden className="size-2 rounded-[2px]" style={{ background: SHADE[k] }} />
                <span className="tabular-nums">{fmt(counts[k])}</span> {LEGEND[k]}
              </li>
            ))}
          </ul>
        </div>
      )}

      <details className="group relative ml-auto">
        <summary className="cursor-pointer rounded-md px-2 py-1 text-ink-2 select-none hover:bg-canvas hover:text-ink group-open:bg-canvas group-open:text-ink">
          How is this checked?
        </summary>
        <div className="absolute top-full right-0 z-30 mt-2 w-[min(26rem,calc(100vw-2rem))] rounded-lg border border-line bg-sheet p-4 leading-relaxed shadow-[0_12px_32px_-12px_rgb(18_22_27/0.25)]">
          <p>
            The model copies a quote from the filing for every claim. Each quote is then
            searched for in the filing&apos;s text:
          </p>
          <ul className="mt-2 space-y-1.5">
            {KINDS.map((k) => (
              <li key={k} className="flex gap-2">
                <span aria-hidden className="mt-1.5 size-2 shrink-0 rounded-[2px]" style={{ background: SHADE[k] }} />
                <span>
                  <span className="font-medium capitalize">{LEGEND[k]}:</span>{" "}
                  <span className="text-ink-2">{EXPLAIN[k]}</span>
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-ink-2">
            A claim whose quote can&apos;t be found is dropped, and listed under its section.
          </p>
        </div>
      </details>
    </section>
  );
}
