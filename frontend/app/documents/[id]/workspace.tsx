"use client";

import Link from "next/link";
import { Fragment, useEffect, useRef, useState } from "react";

import {
  CLAIM_LABELS,
  MATCH_LABELS,
  companyName,
  plural,
  sectionName,
} from "@/app/lib/format";
import { type Mark, segments } from "@/app/lib/highlight";
import type {
  ClaimType,
  DocumentDetail,
  Finding,
  MatchKind,
  Paragraph,
  Section,
} from "@/app/lib/types";

const TYPES: ClaimType[] = ["metric", "guidance", "risk", "event"];
const MATCHES: MatchKind[] = ["exact", "normalized", "elided", "fuzzy"];
const MATCH_SUMMARY: Record<MatchKind, string> = {
  exact: "exact",
  normalized: "exact apart from punctuation or spacing",
  elided: "shortened with every part found in order",
  fuzzy: "close matches",
};

interface Selection {
  findingId: number;
  paragraphId: string;
  // Where the click came from; the other pane scrolls to follow it.
  from: "notes" | "page";
}

interface Props {
  document: DocumentDetail;
  findings: Finding[];
  sections: Section[];
}

function scrollBehavior(): ScrollBehavior {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
}

export default function Workspace({ document, findings, sections }: Props) {
  const [sectionItem, setSectionItem] = useState(sections[0]?.item ?? "");
  const [hidden, setHidden] = useState<ReadonlySet<ClaimType>>(new Set());
  const [selected, setSelected] = useState<Selection | null>(null);
  const notesRef = useRef<HTMLDivElement>(null);
  const pageRef = useRef<HTMLDivElement>(null);

  const section = sections.find((s) => s.item === sectionItem);
  const verified = findings.filter((f) => f.verified);
  const inSection = verified.filter((f) => f.section === sectionItem);
  const visible = inSection.filter((f) => !hidden.has(f.type));
  const dropped = findings.filter((f) => !f.verified && f.section === sectionItem);
  const positions = new Map(section?.paragraphs.map((p) => [p.id, p.position]));

  const marks = new Map<string, Mark[]>();
  for (const f of visible) {
    for (const c of f.citations) {
      if (c.match === null || c.start === null || c.end === null) continue;
      const list = marks.get(c.paragraph_id) ?? [];
      list.push({ findingId: f.id, type: f.type, match: c.match, start: c.start, end: c.end });
      marks.set(c.paragraph_id, list);
    }
  }

  useEffect(() => {
    if (selected === null) return;
    const pane = selected.from === "notes" ? pageRef.current : notesRef.current;
    const selector =
      selected.from === "notes"
        ? `[data-paragraph="${CSS.escape(selected.paragraphId)}"]`
        : `[data-finding="${selected.findingId}"]`;
    pane?.querySelector(selector)?.scrollIntoView({
      block: selected.from === "notes" ? "center" : "nearest",
      behavior: scrollBehavior(),
    });
  }, [selected]);

  function showSection(item: string) {
    setSectionItem(item);
    setSelected(null);
    pageRef.current?.scrollTo({ top: 0 });
    notesRef.current?.scrollTo({ top: 0 });
  }

  function toggleType(type: ClaimType) {
    setHidden((current) => {
      const next = new Set(current);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  }

  return (
    <div className="flex flex-col lg:h-dvh">
      <Header document={document} findings={findings} />

      <div className="flex flex-wrap items-center gap-x-8 gap-y-3 border-b border-rule px-6 py-3">
        <div role="group" aria-label="Section" className="flex gap-1 rounded-lg bg-ink/5 p-1">
          {sections.map((s) => {
            const count = verified.filter((f) => f.section === s.item).length;
            const pressed = s.item === sectionItem;
            return (
              <button
                key={s.item}
                type="button"
                aria-pressed={pressed}
                onClick={() => showSection(s.item)}
                className={
                  pressed
                    ? "rounded-md bg-page px-3 py-1.5 text-sm font-medium shadow-sheet"
                    : "rounded-md px-3 py-1.5 text-sm text-ink-soft hover:text-ink"
                }
              >
                {sectionName(s.item, s.title)}{" "}
                <span className="tabular-nums text-ink-soft">{count}</span>
              </button>
            );
          })}
        </div>

        <div role="group" aria-label="Claim types" className="flex flex-wrap gap-2">
          {TYPES.map((type) => {
            const count = inSection.filter((f) => f.type === type).length;
            const on = !hidden.has(type);
            return (
              <button
                key={type}
                type="button"
                aria-pressed={on}
                onClick={() => toggleType(type)}
                className={`flex items-center gap-2 rounded-full border px-3 py-1 text-sm ${
                  on ? "border-ink/25 bg-page" : "border-rule text-ink-soft"
                }`}
              >
                <span
                  aria-hidden
                  className="size-3 rounded-sm border border-ink/20"
                  style={{ background: on ? `var(--color-${type})` : "transparent" }}
                />
                {CLAIM_LABELS[type]}
                <span className="tabular-nums text-ink-soft">{count}</span>
              </button>
            );
          })}
        </div>
      </div>

      <div className="grid min-h-0 flex-1 lg:grid-cols-[minmax(22rem,5fr)_minmax(0,7fr)]">
        <div
          ref={notesRef}
          className="min-h-0 border-rule px-4 py-4 lg:overflow-y-auto lg:border-r"
        >
          {visible.length === 0 ? (
            <p className="px-2 py-6 text-ink-soft">
              {inSection.length === 0
                ? "No claims were extracted from this section."
                : "No claims of the selected types in this section. Turn a type back on above."}
            </p>
          ) : (
            <ul className="space-y-1">
              {visible.map((f) => (
                <Note
                  key={f.id}
                  finding={f}
                  positions={positions}
                  active={f.id === selected?.findingId}
                  onSelect={(paragraphId) =>
                    setSelected({ findingId: f.id, paragraphId, from: "notes" })
                  }
                />
              ))}
            </ul>
          )}

          <p className="mt-6 px-2 text-sm leading-relaxed text-ink-soft">
            The underline under a paragraph number shows how the quote matched the filing:
            solid for exact, dashed for a shortened quote whose parts were all found in
            order, dotted for a close match.
          </p>

          {dropped.length > 0 && (
            <details className="mt-4 px-2 text-sm">
              <summary className="cursor-pointer text-ink-soft">
                {plural(dropped.length, "claim")} dropped because the quote wasn&apos;t in the
                filing
              </summary>
              <ul className="mt-3 space-y-3">
                {dropped.map((f) => (
                  <li key={f.id}>
                    <p>{f.claim}</p>
                    <p className="mt-1 font-serif text-ink-soft">
                      Quoted as &ldquo;{f.citations[0]?.quote}&rdquo;
                    </p>
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>

        <div ref={pageRef} className="min-h-0 px-3 py-6 sm:px-6 lg:overflow-y-auto">
          {section ? (
            <article className="mx-auto max-w-[46rem] rounded-sm bg-page px-5 py-10 shadow-sheet sm:px-10">
              <h2 className="mb-8 pl-12 font-serif text-xl font-bold">
                Item {section.item}. {sectionName(section.item, section.title)}
              </h2>
              <div className="space-y-4">
                {section.paragraphs.map((p) => (
                  <SourceParagraph
                    key={p.id}
                    paragraph={p}
                    marks={marks.get(p.id) ?? []}
                    selectedId={selected?.findingId ?? null}
                    current={p.id === selected?.paragraphId}
                    onSelect={(findingId) =>
                      setSelected({ findingId, paragraphId: p.id, from: "page" })
                    }
                  />
                ))}
              </div>
            </article>
          ) : (
            <p className="mx-auto max-w-[46rem] text-ink-soft">
              This filing hasn&apos;t been analyzed yet.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

function Header({ document, findings }: { document: DocumentDetail; findings: Finding[] }) {
  const kept = findings.filter((f) => f.verified);
  const dropped = findings.length - kept.length;
  const counts = Object.fromEntries(MATCHES.map((m) => [m, 0])) as Record<MatchKind, number>;
  for (const f of kept) for (const c of f.citations) if (c.match) counts[c.match] += 1;
  const breakdown = MATCHES.filter((m) => counts[m] > 0)
    .map((m) => `${counts[m].toLocaleString("en-US")} ${MATCH_SUMMARY[m]}`)
    .join(", ");
  const tokens = document.runs.reduce((n, r) => n + r.prompt_tokens + r.completion_tokens, 0);
  const calls = document.runs.reduce((n, r) => n + r.llm_calls, 0);
  const models = [...new Set(document.runs.map((r) => r.model))].join(" and ");
  const filed = new Date(`${document.filed}T00:00:00`).toLocaleDateString("en-US", {
    dateStyle: "long",
  });

  return (
    <header className="border-b border-rule px-6 pb-5 pt-4">
      <Link href="/" className="text-sm text-ink-soft underline-offset-4 hover:text-ink hover:underline">
        All filings
      </Link>
      <h1 className="mt-2 text-2xl font-semibold tracking-tight">{companyName(document.company)}</h1>
      <p className="mt-1 text-ink-soft">
        {document.form} for fiscal {document.fiscal_year}, filed {filed}.{" "}
        <a href={document.url} className="underline underline-offset-4 hover:text-ink">
          Read the original on SEC.gov
        </a>
      </p>
      {findings.length > 0 && (
        <p className="mt-3 max-w-[80ch] leading-relaxed">
          {dropped === 0
            ? `${plural(kept.length, "claim")}, and every quote was found in the filing: ${breakdown}.`
            : `${plural(kept.length, "claim")} kept, ${dropped} dropped because the quote wasn't in the filing. Kept quotes: ${breakdown}.`}{" "}
          <span className="text-ink-soft">
            Extracted by {models} in {plural(calls, "call")} using{" "}
            {tokens.toLocaleString("en-US")} tokens.
          </span>
        </p>
      )}
    </header>
  );
}

function Note({
  finding,
  positions,
  active,
  onSelect,
}: {
  finding: Finding;
  positions: Map<string, number>;
  active: boolean;
  onSelect: (paragraphId: string) => void;
}) {
  const first = finding.citations[0];
  return (
    <li
      data-finding={finding.id}
      className={`relative rounded-md py-3 pl-5 pr-3 ${active ? "bg-page shadow-sheet" : ""}`}
    >
      <span
        aria-hidden
        className="absolute bottom-3 left-1.5 top-3 w-1 rounded-full"
        style={{ background: `var(--color-${finding.type})` }}
      />
      <button
        type="button"
        onClick={() => onSelect(first.paragraph_id)}
        className="w-full text-left leading-snug hover:underline hover:decoration-ink/30 hover:underline-offset-4"
      >
        {finding.claim}
      </button>
      <div className="mt-1.5 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm text-ink-soft">
        <span>{CLAIM_LABELS[finding.type]}</span>
        {finding.citations.map((c, i) =>
          c.match ? (
            <button
              key={i}
              type="button"
              onClick={() => onSelect(c.paragraph_id)}
              aria-label={`Show paragraph ${positions.get(c.paragraph_id)}: ${MATCH_LABELS[c.match].toLowerCase()}`}
              title={MATCH_LABELS[c.match]}
              className={`match-${c.match} decoration-ink/40 hover:text-ink`}
            >
              Paragraph {positions.get(c.paragraph_id) ?? "?"}
            </button>
          ) : null,
        )}
      </div>
      {active &&
        finding.citations.some((c) => c.match && c.match !== "exact") && (
          <p className="mt-2 font-serif text-sm leading-relaxed text-ink-soft">
            The model quoted it as &ldquo;
            {finding.citations.find((c) => c.match !== "exact")?.quote}&rdquo;
          </p>
        )}
    </li>
  );
}

function SourceParagraph({
  paragraph,
  marks,
  selectedId,
  current,
  onSelect,
}: {
  paragraph: Paragraph;
  marks: Mark[];
  selectedId: number | null;
  current: boolean;
  onSelect: (findingId: number) => void;
}) {
  let body;
  if (paragraph.kind === "table") {
    body = <SourceTable text={paragraph.text} />;
  } else {
    const parts = segments(paragraph.text, marks, selectedId).map((part, i) =>
      part.mark ? (
        <mark
          key={i}
          className="hl"
          data-type={part.mark.type}
          data-active={part.mark.findingId === selectedId ? "" : undefined}
          onClick={() => onSelect(part.mark!.findingId)}
        >
          {part.text}
        </mark>
      ) : (
        <Fragment key={i}>{part.text}</Fragment>
      ),
    );
    body =
      paragraph.kind === "heading" ? (
        <h3 className="font-serif font-bold leading-relaxed">{parts}</h3>
      ) : (
        <p className="font-serif text-[0.9375rem] leading-[1.8]">{parts}</p>
      );
  }

  return (
    <div data-paragraph={paragraph.id} className="grid grid-cols-[2.25rem_1fr] gap-3">
      <span
        aria-hidden
        className="select-none pt-1 text-right text-xs tabular-nums text-ink-soft/80"
      >
        {paragraph.position}
      </span>
      <div className={`border-l-2 pl-4 ${current ? "border-ink" : "border-transparent"}`}>
        {body}
      </div>
    </div>
  );
}

function SourceTable({ text }: { text: string }) {
  const rows = text.split("\n").map((row) => row.split(" | "));
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm tabular-nums">
        <tbody>
          {rows.map((cells, r) => (
            <tr key={r} className="border-b border-rule/60 last:border-0">
              {cells.map((cell, c) => (
                <td key={c} className={`py-1 pr-4 ${c === 0 ? "text-left" : "text-right"}`}>
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
