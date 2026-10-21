import Link from "next/link";

import { CLAIM_LABELS, plural, sectionShort, timestamp } from "@/app/lib/format";
import type { CallChecks, CallClaim, ClaimCheck } from "@/app/lib/types";

const LINK = "underline decoration-line-strong underline-offset-4 hover:decoration-ink";
// "the three 10-K passages", not "the 3 10-K passages".
const WORDS: Record<number, string> = { 1: "one", 2: "two", 3: "three", 4: "four", 5: "five" };

function filingHref(check: ClaimCheck): string | null {
  if (check.document_id === null || !check.section) return null;
  const p = check.paragraph_id ? `&p=${encodeURIComponent(check.paragraph_id)}` : "";
  return `/documents/${check.document_id}?item=${check.section}${p}`;
}

// How one claim fared across its candidate passages, in a few words.
function outcome(claim: CallClaim): { text: string; tone: string } {
  if (claim.checks.length === 0) return { text: "Not checked yet", tone: "text-ink-3" };
  if (claim.checks.some((k) => k.flagged)) return { text: "Contradicts the 10-K", tone: "text-warn" };
  if (claim.checks.some((k) => k.verdict === "consistent" && k.quote))
    return { text: "Consistent with the 10-K", tone: "text-ink-2" };
  return { text: "Not addressed in the 10-K", tone: "text-ink-3" };
}

function Contradiction({ claim, check }: { claim: CallClaim; check: ClaimCheck }) {
  const href = filingHref(check);
  return (
    <li className="grid gap-x-6 gap-y-2 border-t border-line pt-4 sm:grid-cols-2">
      <div>
        <p className="text-xs text-ink-2">
          On the call · {claim.speaker} ·{" "}
          <a href={`?t=${Math.floor(claim.at)}`} className={`font-mono tabular-nums ${LINK}`}>
            {timestamp(claim.at)}
          </a>
        </p>
        <blockquote className="mt-1 font-serif text-[0.9375rem] leading-[1.7]">“{claim.quote}”</blockquote>
      </div>
      <div>
        <p className="text-xs text-ink-2">
          In the 10-K ·{" "}
          {href ? (
            <Link href={href} className={LINK}>
              {check.section ? sectionShort(check.section) : check.title}
            </Link>
          ) : (
            check.title
          )}
        </p>
        <blockquote className="mt-1 font-serif text-[0.9375rem] leading-[1.7]">“{check.quote}”</blockquote>
      </div>
      {check.reason && (
        <p className="max-w-[70ch] text-[0.8125rem] leading-relaxed text-ink-2 sm:col-span-2">{check.reason}</p>
      )}
    </li>
  );
}

export default function Checks({ checks }: { checks: CallChecks }) {
  const { claims } = checks;
  if (claims.length === 0) return null;
  const flagged = claims.flatMap((claim) => claim.checks.filter((k) => k.flagged).map((check) => ({ claim, check })));
  const passages = Math.max(0, ...claims.map((c) => c.checks.length));

  return (
    <section aria-labelledby="checks" className="mt-8 border-y border-line py-5">
      <h2 id="checks" className="font-medium">
        Checked against the 10-K
      </h2>
      <p className="mt-1 max-w-[70ch] leading-relaxed text-ink-2">
        {checks.checked < claims.length && <>{checks.checked.toLocaleString("en-US")} of </>}
        {plural(claims.length, "claim")} by the company&apos;s speakers
        {checks.checked > 0 && (
          <>
            , each judged against the {WORDS[passages] ?? passages} 10-K{" "}
            {passages === 1 ? "passage" : "passages"} that search finds closest
            {checks.verifier_model && (
              <>
                {" "}
                by <span className="font-mono text-xs text-ink">{checks.verifier_model}</span>
              </>
            )}
          </>
        )}
        .{" "}
        {checks.checked > 0 &&
          (flagged.length === 0 ? (
            "None contradicts it."
          ) : (
            <span className="text-warn">{plural(flagged.length, "contradiction")} flagged.</span>
          ))}
      </p>

      {flagged.length > 0 && (
        <ol className="mt-4 space-y-4">
          {flagged.map(({ claim, check }) => (
            <Contradiction key={`${claim.id}-${check.rank}`} claim={claim} check={check} />
          ))}
        </ol>
      )}

      <details className="mt-4">
        <summary className="cursor-pointer text-ink-2 select-none hover:text-ink">
          All {plural(claims.length, "claim")}
        </summary>
        <ol className="mt-3 divide-y divide-line">
          {claims.map((claim) => {
            const { text, tone } = outcome(claim);
            return (
              <li key={claim.id} className="grid gap-x-4 gap-y-0.5 py-2 sm:grid-cols-[3.5rem_minmax(0,1fr)_11rem]">
                <a
                  href={`?t=${Math.floor(claim.at)}`}
                  className="font-mono text-2xs text-ink-3 tabular-nums hover:text-ink sm:pt-0.5"
                >
                  {timestamp(claim.at)}
                </a>
                <p className="text-[0.8125rem] leading-snug">
                  <span className="text-ink-2">
                    {CLAIM_LABELS[claim.type]} · {claim.speaker}:
                  </span>{" "}
                  {claim.claim}
                </p>
                <p className={`text-xs sm:text-right ${tone}`}>{text}</p>
              </li>
            );
          })}
        </ol>
      </details>
    </section>
  );
}
