"use client";

import { Fragment, memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";

import {
  CLAIM_LABELS,
  MATCH_NOTES,
  companyName,
  plural,
  sectionName,
  sectionShort,
} from "@/app/lib/format";
import { type Mark, segments } from "@/app/lib/highlight";
import type { ClaimType, DocumentDetail, Finding, Paragraph, Section } from "@/app/lib/types";
import TopBar, { AnalyzeLink, Crumb } from "@/app/top-bar";
import Funnel from "./funnel";

const TYPES: ClaimType[] = ["metric", "guidance", "risk", "event"];

interface Props {
  document: DocumentDetail;
  findings: Finding[];
  sections: Section[];
  // From ?claim= and ?item=, so a link can open a section or a claim.
  initialClaim: number | null;
  initialItem: string | null;
}

// A claim as the margin shows it: numbered in filing order within its section.
interface Note {
  finding: Finding;
  n: number;
}

// Filings set some headings in capitals; the outline shows them in sentence case.
function outlineLabel(text: string): string {
  if (/[a-z]/.test(text) || !/[A-Z]/.test(text)) return text;
  const lower = text.toLowerCase();
  return lower[0].toUpperCase() + lower.slice(1);
}

// "Risk factors" reads "in risk factors"; acronyms like MD&A keep their capitals.
function inSentence(name: string): string {
  return name === name.toUpperCase() ? name : name.toLowerCase();
}

// Filings often run a bullet straight into its text ("•Overview"). The bullet can be
// a segment of its own when a quote starts right after it.
const BULLET = /^[•●▪◦](?=\S)/;

function spaceBullet(text: string, first: boolean, tight: boolean): React.ReactNode {
  if (!first || !tight) return text;
  return (
    <>
      <span className="pr-[0.4em]">{text[0]}</span>
      {text.slice(1)}
    </>
  );
}

function smooth(): ScrollBehavior {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
}

export default function Reader({ document, findings, sections, initialClaim, initialItem }: Props) {
  const linked = findings.find((f) => f.verified && f.id === initialClaim);
  const [item, setItem] = useState(
    linked?.section ??
      sections.find((s) => s.item === initialItem)?.item ??
      sections[0]?.item ??
      "",
  );
  const [type, setType] = useState<ClaimType | "all">("all");
  const [selected, setSelected] = useState<number | null>(linked?.id ?? null);
  const [peek, setPeek] = useState<number | null>(null);
  // Set when a selection should bring its sentence into view (keys, links).
  const pendingScroll = useRef<"center" | "nearest" | null>(linked ? "center" : null);
  const jumped = useRef(false);
  // Leading paragraphs without claims stay folded until asked for, per view.
  const [unfolded, setUnfolded] = useState<string | null>(null);
  // The outline heading the reader is under.
  const [current, setCurrent] = useState<string | null>(null);

  const section = sections.find((s) => s.item === item);
  const verified = useMemo(() => findings.filter((f) => f.verified), [findings]);
  const inSection = useMemo(() => verified.filter((f) => f.section === item), [verified, item]);
  const dropped = findings.filter((f) => !f.verified && f.section === item);
  const typesHere = TYPES.filter((t) => inSection.some((f) => f.type === t));

  const notes: Note[] = useMemo(
    () =>
      inSection
        .map((finding, i) => ({ finding, n: i + 1 }))
        .filter((note) => type === "all" || note.finding.type === type),
    [inSection, type],
  );

  // Paragraph id -> its quote marks, and the notes that sit beside it.
  const { marks, margin } = useMemo(() => {
    const marks = new Map<string, Mark[]>();
    const margin = new Map<string, Note[]>();
    for (const note of notes) {
      const f = note.finding;
      f.citations.forEach((c, i) => {
        if (c.match === null || c.start === null || c.end === null) return;
        const list = marks.get(c.paragraph_id) ?? [];
        list.push({ findingId: f.id, type: f.type, match: c.match, start: c.start, end: c.end });
        marks.set(c.paragraph_id, list);
        if (i === 0) margin.set(c.paragraph_id, [...(margin.get(c.paragraph_id) ?? []), note]);
      });
    }
    return { marks, margin };
  }, [notes]);

  const positions = useMemo(
    () => new Map(section?.paragraphs.map((p) => [p.id, p.position])),
    [section],
  );

  // Headings with the number of claims noted under each, for the outline.
  const outline = useMemo(() => {
    const out: { paragraph: Paragraph; count: number }[] = [];
    for (const p of section?.paragraphs ?? []) {
      if (p.kind === "heading") out.push({ paragraph: p, count: 0 });
      else if (out.length > 0) out[out.length - 1].count += margin.get(p.id)?.length ?? 0;
    }
    return out.filter((o) => o.count > 0);
  }, [section, margin]);

  // Paragraphs before the first one with a claim fold into one line, so the first
  // claim opens in view. Only two or more: a fold for one costs more than it saves.
  const view = `${item}-${type}`;
  const firstNoted = section?.paragraphs.findIndex((p) => margin.has(p.id)) ?? -1;
  const folded = firstNoted >= 2 && unfolded !== view ? firstNoted : 0;

  // Track the heading above the top of the reading area as the page scrolls.
  useEffect(() => {
    let frame = 0;
    function update() {
      frame = 0;
      let at: string | null = null;
      for (const { paragraph } of outline) {
        const el = globalThis.document.querySelector(
          `[data-paragraph="${CSS.escape(paragraph.id)}"]`,
        );
        // Not rendered means folded away above the first claim: already passed.
        if (el && el.getBoundingClientRect().top > window.innerHeight / 2) break;
        at = paragraph.id;
      }
      setCurrent(at);
    }
    function onScroll() {
      if (!frame) frame = requestAnimationFrame(update);
    }
    globalThis.document.addEventListener("scroll", onScroll, { capture: true, passive: true });
    frame = requestAnimationFrame(update);
    return () => {
      globalThis.document.removeEventListener("scroll", onScroll, { capture: true });
      cancelAnimationFrame(frame);
    };
  }, [outline, folded]);

  function jumpTo(paragraphId: string) {
    const index = section?.paragraphs.findIndex((p) => p.id === paragraphId) ?? -1;
    // A heading inside the fold unfolds it first.
    if (index >= 0 && index < folded) flushSync(() => setUnfolded(view));
    setCurrent(paragraphId);
    globalThis.document
      .querySelector(`[data-paragraph="${CSS.escape(paragraphId)}"]`)
      ?.scrollIntoView({ block: "start", behavior: smooth() });
  }

  const select = useCallback(
    (id: number | null, scroll: "center" | "nearest" | null = null) => {
      pendingScroll.current = scroll;
      setSelected(id);
      window.history.replaceState(null, "", id === null ? `?item=${item}` : `?claim=${id}`);
    },
    [item],
  );

  // Bring the selected claim's sentence into view after it renders.
  useEffect(() => {
    const block = pendingScroll.current;
    pendingScroll.current = null;
    if (selected === null || block === null) return;
    const finding = verified.find((f) => f.id === selected);
    const paragraphId = finding?.citations.find((c) => c.match)?.paragraph_id;
    if (!paragraphId) return;
    // A claim opened from a link jumps there; later selections glide.
    const behavior = jumped.current ? smooth() : "auto";
    jumped.current = true;
    globalThis.document
      .querySelector(`[data-paragraph="${CSS.escape(paragraphId)}"]`)
      ?.scrollIntoView({ block, behavior });
  }, [selected, verified]);

  function showSection(next: string) {
    setItem(next);
    setType("all");
    setSelected(null);
    window.history.replaceState(null, "", `?item=${next}`);
    globalThis.document.getElementById("main")?.scrollTo({ top: 0 });
    window.scrollTo({ top: 0 });
  }

  // J and K step through the claims in the margin; Escape clears the selection.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if ((event.target as HTMLElement).closest("input, select, textarea, [contenteditable]")) return;
      if (event.key === "Escape") {
        if (selected !== null) select(null);
        return;
      }
      if (event.key !== "j" && event.key !== "k") return;
      if (notes.length === 0) return;
      event.preventDefault();
      const at = notes.findIndex((n) => n.finding.id === selected);
      const next =
        event.key === "j"
          ? Math.min(notes.length - 1, at + 1)
          : at === -1
            ? 0
            : Math.max(0, at - 1);
      select(notes[next].finding.id, "center");
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [notes, selected, select]);

  const company = companyName(document.company);
  const tokens = document.runs.reduce((n, r) => n + r.prompt_tokens + r.completion_tokens, 0);
  const calls = document.runs.reduce((n, r) => n + r.llm_calls, 0);
  const models = [...new Set(document.runs.map((r) => r.model.replace(/^[^/]+\//, "")))].join(", ");

  return (
    <>
      <TopBar
        actions={
          <>
            {calls > 0 && (
              <span className="hidden text-ink-2 xl:inline">
                {models}, {plural(calls, "call")},{" "}
                <span className="tabular-nums">{tokens.toLocaleString("en-US")}</span> tokens
              </span>
            )}
            <a
              href={document.url}
              className="hidden items-center gap-1 text-ink-2 hover:text-ink sm:inline-flex"
            >
              Original on SEC.gov
              <svg viewBox="0 0 16 16" aria-hidden className="size-3">
                <path
                  d="M6 3h7v7M13 3 5 11"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </a>
            <span className="hidden md:inline-flex">
              <AnalyzeLink />
            </span>
          </>
        }
      >
        <Crumb href="/">Library</Crumb>
        <Crumb>
          {company}{" "}
          <span className="font-mono text-xs font-normal text-ink-2">
            {document.form} FY{document.fiscal_year}
          </span>
        </Crumb>
        {sections.length > 1 && (
          <div role="group" aria-label="Section" className="ml-3 hidden gap-0.5 sm:flex">
            {sections.map((s) => {
              const count = verified.filter((f) => f.section === s.item).length;
              const on = s.item === item;
              return (
                <button
                  key={s.item}
                  type="button"
                  aria-pressed={on}
                  onClick={() => showSection(s.item)}
                  className={`flex items-center gap-1.5 rounded-md px-2.5 py-1 ${
                    on ? "bg-ink text-sheet" : "text-ink-2 hover:bg-canvas hover:text-ink"
                  }`}
                >
                  {sectionShort(s.item, s.title)}
                  <span className={`font-mono text-2xs tabular-nums ${on ? "text-sheet/70" : "text-ink-3"}`}>
                    {count}
                  </span>
                </button>
              );
            })}
          </div>
        )}
      </TopBar>

      <Funnel findings={findings} />

      <div className="mx-auto flex max-w-[84rem] gap-6 px-3 py-6 sm:px-6">
        {outline.length > 0 && (
          <nav
            aria-label={`Headings in ${sectionName(item)}`}
            className="sticky top-[4.5rem] hidden max-h-[calc(100dvh-6rem)] w-52 shrink-0 self-start overflow-y-auto pr-1 min-[1400px]:block"
          >
            <h2 className="px-2 pb-2 text-xs text-ink-2">In {inSentence(sectionShort(item))}</h2>
            <Outline outline={outline} current={current} onJump={jumpTo} />
            <p className="mt-4 border-t border-line px-2 pt-3 text-xs leading-loose text-ink-2">
              <kbd>J</kbd> <kbd>K</kbd> next and previous claim
              <br />
              <kbd>Esc</kbd> clear
            </p>
          </nav>
        )}

        <article className="min-w-0 flex-1 rounded-lg border border-line bg-sheet px-4 pt-9 pb-16 sm:px-8">
          {section ? (
            <>
              <header className="md:ml-[3.5rem]">
                <h1 className="font-serif text-[1.75rem] leading-tight font-medium tracking-[-0.01em]">
                  {sectionName(section.item, section.title)}
                </h1>
                <p className="mt-2 max-w-[62ch] leading-relaxed text-ink-2">
                  Item {section.item} of {company}&apos;s {document.form} for fiscal{" "}
                  {document.fiscal_year}, filed {filedOn(document.filed)}.{" "}
                  {inSection.length > 0
                    ? `${plural(inSection.length, "claim")} in the margin, each beside the paragraph it quotes.`
                    : "No claims were extracted from this section."}
                </p>
                {sections.length > 1 && (
                  <div role="group" aria-label="Section" className="mt-4 flex gap-1 sm:hidden">
                    {sections.map((s) => (
                      <button
                        key={s.item}
                        type="button"
                        aria-pressed={s.item === item}
                        onClick={() => showSection(s.item)}
                        className="rounded-md border border-line px-2.5 py-1 aria-pressed:border-ink aria-pressed:bg-ink aria-pressed:text-sheet"
                      >
                        {sectionShort(s.item, s.title)}
                      </button>
                    ))}
                  </div>
                )}
                <div className="mt-5 flex flex-wrap items-start gap-2 empty:hidden">
                  {typesHere.length > 1 && (
                    <div
                      role="group"
                      aria-label="Claim type"
                      className="inline-flex flex-wrap gap-0.5 rounded-lg border border-line p-0.5"
                    >
                      {(["all", ...typesHere] as const).map((t) => {
                        const count =
                          t === "all" ? inSection.length : inSection.filter((f) => f.type === t).length;
                        return (
                          <button
                            key={t}
                            type="button"
                            aria-pressed={type === t}
                            onClick={() => {
                              setType(t);
                              select(null);
                            }}
                            className="flex items-center gap-1.5 rounded-md px-2.5 py-1 text-ink-2 hover:text-ink aria-pressed:bg-canvas aria-pressed:font-medium aria-pressed:text-ink"
                          >
                            {t === "all" ? "All" : CLAIM_LABELS[t]}
                            <span className="font-mono text-2xs text-ink-3 tabular-nums">{count}</span>
                          </button>
                        );
                      })}
                    </div>
                  )}
                  {outline.length > 0 && (
                    <HeadingsMenu outline={outline} current={current} onJump={jumpTo} />
                  )}
                </div>
                {dropped.length > 0 && <Dropped findings={dropped} />}
              </header>

              <div className="mt-8 space-y-4">
                {folded > 0 && (
                  <div className={ROW_GRID}>
                    <span className="pt-2 text-right font-mono text-2xs text-ink-3 tabular-nums">
                      {section.paragraphs[0].position}–{section.paragraphs[folded - 1].position}
                    </span>
                    <button
                      type="button"
                      onClick={() => setUnfolded(view)}
                      className="flex items-center gap-2 border-y border-dashed border-line-strong py-1.5 text-left text-ink-2 hover:text-ink"
                    >
                      <svg viewBox="0 0 16 16" aria-hidden className="size-3.5 shrink-0">
                        <path
                          d="m4 6 4 4 4-4"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.5"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                      <span>
                        {plural(folded, "paragraph")} before the first claim
                        {foldedHeadings(section.paragraphs.slice(0, folded))}.{" "}
                        <span className="font-medium text-ink">Show them</span>
                      </span>
                    </button>
                  </div>
                )}
                {section.paragraphs.slice(folded).map((p) => {
                  const here = margin.get(p.id) ?? [];
                  const pMarks = marks.get(p.id) ?? [];
                  const involved =
                    selected !== null &&
                    (pMarks.some((m) => m.findingId === selected) ||
                      here.some((n) => n.finding.id === selected));
                  return (
                    <Row
                      key={p.id}
                      paragraph={p}
                      marks={pMarks}
                      notes={here}
                      positions={positions}
                      selectedId={involved ? selected : null}
                      onSelect={select}
                      onPeek={setPeek}
                    />
                  );
                })}
              </div>
            </>
          ) : (
            <p className="text-ink-2">This filing hasn&apos;t been analyzed yet.</p>
          )}
        </article>
      </div>

      {/* Hovering a note lightly marks its sentence. */}
      {peek !== null && peek !== selected && (
        <style>{`mark.quote[data-f="${peek}"]{background-color:var(--color-mark-soft)}`}</style>
      )}
    </>
  );
}

function Dropped({ findings }: { findings: Finding[] }) {
  return (
    <details className="mt-5 max-w-[62ch] rounded-md border border-line px-3 py-2">
      <summary className="cursor-pointer text-ink-2 hover:text-ink">
        <span className="font-medium text-bad">{plural(findings.length, "claim")} dropped</span>{" "}
        because the quote isn&apos;t in the filing
      </summary>
      <ul className="mt-2 space-y-3 pb-1">
        {findings.map((f) => (
          <li key={f.id}>
            <p>{f.claim}</p>
            <p className="mt-0.5 font-serif text-ink-2">
              Quoted as &ldquo;{f.citations[0]?.quote}&rdquo;
            </p>
          </li>
        ))}
      </ul>
    </details>
  );
}

const ROW_GRID =
  "grid scroll-mt-24 grid-cols-[1.75rem_minmax(0,1fr)] gap-x-4 gap-y-2 md:grid-cols-[2rem_minmax(0,36rem)_minmax(13rem,16rem)] md:gap-x-6";

function filedOn(iso: string): string {
  return new Date(`${iso}T00:00:00`).toLocaleDateString("en-US", { dateStyle: "medium" });
}

// So the fold says what it hides.
function foldedHeadings(paragraphs: Paragraph[]): string {
  const headings = paragraphs.filter((p) => p.kind === "heading").length;
  return headings > 0 ? `, including ${plural(headings, "heading")}` : "";
}

interface OutlineItem {
  paragraph: Paragraph;
  count: number;
}

function Outline({
  outline,
  current,
  onJump,
}: {
  outline: OutlineItem[];
  current: string | null;
  onJump: (paragraphId: string) => void;
}) {
  return (
    <ol className="space-y-px">
      {outline.map(({ paragraph, count }) => {
        const here = paragraph.id === current;
        return (
          <li key={paragraph.id}>
            <button
              type="button"
              aria-current={here ? "location" : undefined}
              onClick={() => onJump(paragraph.id)}
              className={`grid w-full grid-cols-[1fr_auto] gap-2 rounded-md px-2 py-1.5 text-left text-xs leading-snug ${
                here ? "bg-accent-tint text-accent" : "text-ink-2 hover:bg-canvas hover:text-ink"
              }`}
            >
              <span className="line-clamp-2">{outlineLabel(paragraph.text)}</span>
              <span
                className={`font-mono text-2xs tabular-nums ${here ? "text-accent" : "text-ink-3"}`}
              >
                {count}
              </span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}

// The outline as a menu, for screens too narrow for its column.
function HeadingsMenu({
  outline,
  current,
  onJump,
}: {
  outline: OutlineItem[];
  current: string | null;
  onJump: (paragraphId: string) => void;
}) {
  const menu = useRef<HTMLDetailsElement>(null);
  return (
    <details ref={menu} className="group relative min-[1400px]:hidden">
      <summary className="flex h-[2.125rem] cursor-pointer items-center gap-1.5 rounded-lg border border-line px-3 text-ink-2 select-none hover:text-ink group-open:text-ink">
        Headings
        <span className="font-mono text-2xs text-ink-3 tabular-nums">{outline.length}</span>
        <svg
          viewBox="0 0 16 16"
          aria-hidden
          className="size-3.5 transition-transform group-open:rotate-180"
        >
          <path
            d="m4 6 4 4 4-4"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </summary>
      <div className="absolute top-full left-0 z-30 mt-2 max-h-[60vh] w-[min(22rem,calc(100vw-3rem))] overflow-y-auto rounded-lg border border-line bg-sheet p-1.5 shadow-[0_12px_32px_-12px_rgb(18_22_27/0.25)]">
        <Outline
          outline={outline}
          current={current}
          onJump={(id) => {
            if (menu.current) menu.current.open = false;
            onJump(id);
          }}
        />
      </div>
    </details>
  );
}

interface RowProps {
  paragraph: Paragraph;
  marks: Mark[];
  notes: Note[];
  positions: Map<string, number>;
  // The selected claim, only when this row shows it; other rows skip re-rendering.
  selectedId: number | null;
  onSelect: (id: number | null, scroll?: "center" | "nearest" | null) => void;
  onPeek: (id: number | null) => void;
}

const Row = memo(function Row({
  paragraph,
  marks,
  notes,
  positions,
  selectedId,
  onSelect,
  onPeek,
}: RowProps) {
  let body;
  const tight = BULLET.test(paragraph.text);
  if (paragraph.kind === "table") {
    body = <SourceTable text={paragraph.text} />;
  } else {
    const parts = segments(paragraph.text, marks, selectedId).map((part, i) =>
      part.mark ? (
        <mark
          key={i}
          className="quote"
          data-f={part.mark.findingId}
          data-active={part.mark.findingId === selectedId ? "" : undefined}
          onClick={() => {
            const id = part.mark!.findingId;
            onSelect(id === selectedId ? null : id);
          }}
        >
          {spaceBullet(part.text, i === 0, tight)}
        </mark>
      ) : (
        <Fragment key={i}>{spaceBullet(part.text, i === 0, tight)}</Fragment>
      ),
    );
    body =
      paragraph.kind === "heading" ? (
        <h2 className="pt-3 font-serif text-base leading-relaxed font-semibold">{parts}</h2>
      ) : (
        <p className="font-serif text-base leading-[1.75] text-ink">{parts}</p>
      );
  }

  return (
    <div data-paragraph={paragraph.id} className={ROW_GRID}>
      <span
        aria-hidden
        className={`pt-1.5 text-right font-mono text-2xs tabular-nums select-none ${
          selectedId !== null ? "font-medium text-ink" : "text-ink-3"
        }`}
      >
        {paragraph.position}
      </span>
      <div className="min-w-0">{body}</div>
      {notes.length > 0 && (
        <ul className="col-start-2 space-y-1 md:col-start-3 md:-mt-0.5">
          {notes.map((note) => (
            <li key={note.finding.id}>
              <MarginNote
                note={note}
                active={note.finding.id === selectedId}
                positions={positions}
                onSelect={onSelect}
                onPeek={onPeek}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
});

function MarginNote({
  note,
  active,
  positions,
  onSelect,
  onPeek,
}: {
  note: Note;
  active: boolean;
  positions: Map<string, number>;
  onSelect: (id: number | null, scroll?: "center" | "nearest" | null) => void;
  onPeek: (id: number | null) => void;
}) {
  const { finding, n } = note;
  const first = finding.citations.find((c) => c.match);
  const qualifier = first?.match ? MATCH_NOTES[first.match] : null;
  const others = finding.citations.filter((c) => c.match && c !== first);
  return (
    <div
      className={`rounded-md transition-colors duration-150 ${active ? "bg-mark-soft" : "hover:bg-canvas"}`}
    >
      <button
        type="button"
        aria-pressed={active}
        onClick={() => onSelect(active ? null : finding.id, active ? null : "nearest")}
        onMouseEnter={() => onPeek(finding.id)}
        onMouseLeave={() => onPeek(null)}
        onFocus={() => onPeek(finding.id)}
        onBlur={() => onPeek(null)}
        className="grid w-full grid-cols-[1.25rem_minmax(0,1fr)] gap-x-2 px-2 py-1.5 text-left"
      >
        <span
          aria-hidden
          className={`mt-px grid size-5 place-items-center rounded-full border font-mono text-2xs tabular-nums ${
            active ? "border-mark-line bg-mark text-ink" : "border-line-strong bg-sheet text-ink-2"
          }`}
        >
          {n}
        </span>
        <span className="text-xs leading-5 text-ink-2">
          <span className="sr-only">Claim {n}, </span>
          {CLAIM_LABELS[finding.type]}
          {qualifier && <span className="text-warn"> · {qualifier}</span>}
        </span>
        <span className="col-start-2 text-[0.8125rem] leading-snug text-ink">{finding.claim}</span>
      </button>
      {active && (qualifier || others.length > 0) && (
        <div className="space-y-1.5 px-2 pb-2 pl-9 text-xs leading-relaxed text-ink-2">
          {qualifier && first && (
            <p>
              The model quoted it as{" "}
              <span className="font-serif text-[0.8125rem] text-ink">&ldquo;{first.quote}&rdquo;</span>
            </p>
          )}
          {others.length > 0 && (
            <p>
              Also quoted in paragraph{others.length > 1 ? "s" : ""}{" "}
              {others.map((c, i) => (
                <Fragment key={i}>
                  {i > 0 && ", "}
                  <button
                    type="button"
                    className="underline decoration-line-strong underline-offset-2 hover:text-ink"
                    onClick={() =>
                      globalThis.document
                        .querySelector(`[data-paragraph="${CSS.escape(c.paragraph_id)}"]`)
                        ?.scrollIntoView({ block: "center", behavior: smooth() })
                    }
                  >
                    {positions.get(c.paragraph_id) ?? "?"}
                  </button>
                </Fragment>
              ))}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function SourceTable({ text }: { text: string }) {
  const rows = text.split("\n").map((row) => row.split(" | "));
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[0.8125rem] tabular-nums">
        <tbody>
          {rows.map((cells, r) => (
            <tr key={r} className="border-b border-line last:border-0">
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
