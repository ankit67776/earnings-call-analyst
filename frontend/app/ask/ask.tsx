"use client";

import Link from "next/link";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { MATCH_NOTES, companyName, plural, timestamp } from "@/app/lib/format";
import type { ChatAnswer, ChatCitation, ChatSource, Company } from "@/app/lib/types";
import TopBar, { Crumb } from "@/app/top-bar";

// Questions the sources answer, one per kind of source, plus one they don't: the
// quickest way for a first visitor to see both citations and a refusal.
const EXAMPLES: Record<string, string[]> = {
  HSY: [
    "What did management say about Halloween?",
    "How much did net sales grow in 2020?",
    "What was the 2020 dividend increase?",
  ],
  SITM: [
    "How much revenue came from the largest customer?",
    "What did management say about 5G demand?",
    "What was SiTime's revenue guidance for Q4 2021?",
  ],
  COP: [
    "When did the Concho acquisition close?",
    "How much did production fall because of curtailments?",
    "What will capital spending be in 2025?",
  ],
};

interface Exchange {
  id: number;
  ticker: string;
  question: string;
  answer: ChatAnswer | null;
  error: string | null;
}

// Citations are numbered by source, in order of first use in the answer.
function numberCitations(answer: ChatAnswer): Map<string, number> {
  const order = new Map<string, number>();
  for (const s of answer.sentences)
    for (const c of s.citations) if (!order.has(c.label)) order.set(c.label, order.size + 1);
  return order;
}

export default function Ask({
  companies,
  initialTicker,
  initialQuestion,
}: {
  companies: Company[] | null;
  initialTicker: string | null;
  initialQuestion: string | null;
}) {
  const [ticker, setTicker] = useState(
    companies?.find((c) => c.ticker === initialTicker)?.ticker ?? companies?.[0]?.ticker ?? "",
  );
  const [question, setQuestion] = useState("");
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [busy, setBusy] = useState(false);
  // The exchange whose sources are open, and the source label selected in it.
  const [open, setOpen] = useState<{ exchange: number; label: string | null } | null>(null);
  const nextId = useRef(1);
  const input = useRef<HTMLInputElement>(null);
  const company = companies?.find((c) => c.ticker === ticker);

  async function ask(text: string) {
    const q = text.trim();
    if (!q || busy || !company) return;
    const id = nextId.current++;
    setExchanges((list) => [...list, { id, ticker, question: q, answer: null, error: null }]);
    setQuestion("");
    setBusy(true);
    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ticker, question: q }),
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => null);
        throw new Error(
          response.status === 429
            ? "Today's model quota is used up. It resets at midnight UTC."
            : (detail?.detail ?? `The backend returned ${response.status}.`),
        );
      }
      const answer = (await response.json()) as ChatAnswer;
      setExchanges((list) => list.map((e) => (e.id === id ? { ...e, answer } : e)));
      const first = answer.sentences[0]?.citations[0]?.label ?? null;
      setOpen({ exchange: id, label: first });
    } catch (error) {
      const message =
        error instanceof TypeError ? "Can't reach the analysis service." : String((error as Error).message);
      setExchanges((list) => list.map((e) => (e.id === id ? { ...e, error: message } : e)));
    } finally {
      setBusy(false);
      input.current?.focus();
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void ask(question);
  }

  const asked = useRef(false);
  useEffect(() => {
    if (asked.current || !initialQuestion) return;
    asked.current = true;
    void ask(initialQuestion);
    // Once, on arrival.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const openExchange = exchanges.find((e) => e.id === open?.exchange && e.answer);

  return (
    <>
      <TopBar>
        <Crumb href="/">Library</Crumb>
        <Crumb>Ask</Crumb>
      </TopBar>

      <div className="mx-auto grid max-w-[84rem] gap-6 px-4 pt-8 pb-16 sm:px-8 xl:grid-cols-[minmax(0,44rem)_minmax(0,1fr)]">
        <main className="min-w-0">
          <h1 className="text-xl font-semibold tracking-[-0.015em]">Ask</h1>
          <p className="mt-1 max-w-[70ch] leading-relaxed text-ink-2">
            Questions about a company&apos;s 10-K and earnings call. Every sentence of an answer
            links to the passage it quotes, and every quote is checked against that passage.
            When the sources don&apos;t say, the answer says so instead of guessing.
          </p>

          {companies === null ? (
            <p className="mt-6 rounded-lg border border-line bg-sheet px-5 py-4">
              <span className="font-medium">Can&apos;t reach the analysis service.</span>{" "}
              <span className="text-ink-2">Start the backend and reload this page.</span>
            </p>
          ) : companies.length === 0 ? (
            <p className="mt-6 text-ink-2">
              Nothing is indexed yet. Run{" "}
              <code className="font-mono text-[0.8125rem]">python -m app.retrieval index</code> in
              the backend.
            </p>
          ) : (
            <>
              <div
                role="radiogroup"
                aria-label="Company"
                className="mt-6 inline-flex flex-wrap gap-0.5 rounded-lg border border-line bg-sheet p-0.5"
              >
                {companies.map((c) => (
                  <button
                    key={c.ticker}
                    type="button"
                    role="radio"
                    aria-checked={c.ticker === ticker}
                    onClick={() => setTicker(c.ticker)}
                    className={`rounded-md px-2.5 py-1 ${
                      c.ticker === ticker ? "bg-canvas font-medium text-ink" : "text-ink-2 hover:text-ink"
                    }`}
                  >
                    {companyName(c.company)}
                  </button>
                ))}
              </div>
              {company && <Coverage company={company} />}

              <form onSubmit={submit} className="mt-4 flex gap-2">
                <label htmlFor="question" className="sr-only">
                  Question about {company ? companyName(company.company) : "the company"}
                </label>
                <input
                  ref={input}
                  id="question"
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  maxLength={500}
                  autoComplete="off"
                  placeholder={`Ask about ${company ? companyName(company.company) : "a company"}`}
                  className="h-8 min-w-0 flex-1 rounded-md border border-line-strong bg-sheet px-2.5 placeholder:text-ink-3 hover:border-ink-3"
                />
                <button
                  type="submit"
                  disabled={busy || question.trim().length < 3}
                  className="h-8 rounded-md bg-ink px-3 font-medium text-sheet hover:bg-ink/85 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  Ask
                </button>
              </form>

              {exchanges.length === 0 && (
                <div className="mt-4">
                  <p className="text-xs text-ink-2">Try one</p>
                  <ul className="mt-1.5 space-y-1">
                    {(EXAMPLES[ticker] ?? []).map((q) => (
                      <li key={q}>
                        <button
                          type="button"
                          onClick={() => void ask(q)}
                          className="text-left underline decoration-line-strong underline-offset-4 hover:decoration-ink"
                        >
                          {q}
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}

          <ol className="mt-8 space-y-8" aria-live="polite">
            {exchanges.map((e) => (
              <li key={e.id} className="border-t border-line pt-5">
                <p className="font-medium">
                  {e.question}{" "}
                  <span className="font-mono text-xs font-normal text-ink-2">{e.ticker}</span>
                </p>
                {e.error ? (
                  <p className="mt-2 text-bad">{e.error}</p>
                ) : e.answer === null ? (
                  <p className="mt-2 text-ink-2">
                    <span aria-hidden className="pulse mr-2 inline-block size-[7px] rounded-full bg-accent" />
                    Searching the 10-K and the call, then writing an answer from what it finds…
                  </p>
                ) : (
                  <AnswerView
                    answer={e.answer}
                    selected={open?.exchange === e.id ? open.label : null}
                    onSelect={(label) => setOpen({ exchange: e.id, label })}
                  />
                )}
              </li>
            ))}
          </ol>
        </main>

        <aside aria-label="Sources" className="min-w-0 xl:sticky xl:top-[4.5rem] xl:max-h-[calc(100dvh-6rem)] xl:self-start xl:overflow-y-auto">
          {openExchange?.answer && (
            <Sources
              answer={openExchange.answer}
              selected={open?.label ?? null}
              onSelect={(label) => setOpen({ exchange: openExchange.id, label })}
            />
          )}
        </aside>
      </div>
    </>
  );
}

function Coverage({ company }: { company: Company }) {
  const parts = [
    ...company.documents.map((d) => `FY${d.fiscal_year} ${d.form}`),
    ...company.calls.map((c) => `Q${c.quarter} ${c.fiscal_year} earnings call`),
  ];
  return (
    <p className="mt-2 text-xs text-ink-2">
      Searches the {parts.join(" and ")}:{" "}
      <span className="tabular-nums">{plural(company.passages, "passage")}</span>.
    </p>
  );
}

function Badge({ n, active, onClick }: { n: number; active: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      aria-label={`Source ${n}`}
      className={`mx-0.5 inline-flex size-5 -translate-y-px items-center justify-center rounded-full border align-middle font-mono text-2xs tabular-nums ${
        active ? "border-mark-line bg-mark text-ink" : "border-line-strong bg-sheet text-ink-2 hover:text-ink"
      }`}
    >
      {n}
    </button>
  );
}

function AnswerView({
  answer,
  selected,
  onSelect,
}: {
  answer: ChatAnswer;
  selected: string | null;
  onSelect: (label: string) => void;
}) {
  const numbers = numberCitations(answer);
  const v = answer.verification;
  const found = v.quotes - v.dropped_quotes;
  const seconds = ((answer.llm_ms + answer.retrieval_ms) / 1000).toFixed(1);

  if (!answer.answered) {
    return (
      <div className="mt-2">
        <p>
          <span className="font-medium">Not in the sources.</span>{" "}
          {v.unsupported
            ? "The model wrote an answer, but none of its quotes could be found in the passages, so it isn't shown."
            : "The 10-K and the call don't answer this."}
        </p>
        <p className="mt-1 text-ink-2">
          It read {plural(answer.sources.length, "passage")}; the closest are on the right.
        </p>
        <Meta answer={answer} seconds={seconds} />
      </div>
    );
  }

  return (
    <div className="mt-2">
      <p className="max-w-[65ch] leading-relaxed">
        {answer.sentences.map((s, i) => {
          const labels = [...new Set(s.citations.map((c) => c.label))];
          return (
            <span key={i}>
              {i > 0 && " "}
              {s.text}
              {labels.map((label) => (
                <Badge
                  key={label}
                  n={numbers.get(label) ?? 0}
                  active={label === selected}
                  onClick={() => onSelect(label)}
                />
              ))}
            </span>
          );
        })}
      </p>
      <p className="mt-2 text-xs text-ink-2">
        <span className={found === v.quotes ? "text-ok" : undefined}>
          <span className="font-medium tabular-nums">
            {found} of {plural(v.quotes, "quote")}
          </span>{" "}
          found in the sources
        </span>
        {v.dropped_sentences.length > 0 && (
          <>
            {" · "}
            <span className="font-medium text-bad tabular-nums">
              {plural(v.dropped_sentences.length, "sentence")} dropped
            </span>{" "}
            for quoting text that isn&apos;t there
          </>
        )}
      </p>
      <Meta answer={answer} seconds={seconds} />
    </div>
  );
}

function Meta({ answer, seconds }: { answer: ChatAnswer; seconds: string }) {
  return (
    <p className="mt-1 font-mono text-2xs text-ink-3 tabular-nums">
      {answer.model.replace(/^[^/]+\//, "")} · {(answer.prompt_tokens + answer.completion_tokens).toLocaleString("en-US")}{" "}
      tokens · {seconds}s{answer.cached ? " · cached" : ""}
    </p>
  );
}

function Sources({
  answer,
  selected,
  onSelect,
}: {
  answer: ChatAnswer;
  selected: string | null;
  onSelect: (label: string) => void;
}) {
  const numbers = numberCitations(answer);
  const cited = answer.sources.filter((s) => numbers.has(s.label));
  const others = answer.sources.filter((s) => !numbers.has(s.label));
  const citations = answer.sentences.flatMap((s) => s.citations);
  const ref = useRef<HTMLLIElement>(null);

  useEffect(() => {
    ref.current?.scrollIntoView({ block: "nearest" });
  }, [selected]);

  return (
    <div className="rounded-lg border border-line bg-sheet">
      <h2 className="border-b border-line px-4 py-2.5 font-medium">
        {cited.length > 0 ? `${plural(cited.length, "source")} cited` : "Passages it read"}
        <span className="ml-2 text-xs font-normal text-ink-2">
          of {plural(answer.sources.length, "passage")} retrieved
        </span>
      </h2>
      <ol className="divide-y divide-line">
        {[...cited, ...others].map((source) => {
          const n = numbers.get(source.label);
          const isOpen = source.label === selected || (selected === null && source === answer.sources[0]);
          const marks = citations.filter((c) => c.passage_id === source.passage_id);
          return (
            <li key={source.label} ref={isOpen ? ref : undefined} className="px-4 py-3">
              <button
                type="button"
                onClick={() => onSelect(source.label)}
                aria-expanded={isOpen}
                className="flex w-full items-start gap-2.5 text-left"
              >
                {n ? (
                  <span
                    aria-hidden
                    className={`inline-flex size-5 shrink-0 items-center justify-center rounded-full border font-mono text-2xs tabular-nums ${
                      isOpen ? "border-mark-line bg-mark" : "border-line-strong text-ink-2"
                    }`}
                  >
                    {n}
                  </span>
                ) : (
                  <span aria-hidden className="size-5 shrink-0" />
                )}
                <span className="min-w-0">
                  <span className="block font-medium">{sourceName(source)}</span>
                  <span className="block truncate text-xs text-ink-2">{sourceDetail(source)}</span>
                </span>
              </button>
              {isOpen && (
                <div className="mt-2.5 pl-7.5">
                  <Passage text={source.text} marks={marks} />
                  {marks.some((m) => MATCH_NOTES[m.match]) && (
                    <p className="mt-2 text-xs text-warn">
                      Not quoted exactly: {MATCH_NOTES[marks.find((m) => MATCH_NOTES[m.match])!.match]}.
                    </p>
                  )}
                  <OpenLink source={source} citation={marks[0]} />
                </div>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function sourceName(source: ChatSource): string {
  if (source.source === "call") {
    const time = source.start !== null ? ` at ${timestamp(source.start)}` : "";
    return `Earnings call${time}`;
  }
  const item = source.title.match(/^Item (\w+)\./)?.[1];
  return item ? `10-K, Item ${item}` : "10-K";
}

function sourceDetail(source: ChatSource): string {
  if (source.source === "call") return source.speakers?.join(", ") ?? source.title;
  return source.title.replace(/^Item \w+\.\s*/, "");
}

// The passage in the source's own words, with every quote the answer took from it
// underlined and filled.
function Passage({ text, marks }: { text: string; marks: ChatCitation[] }) {
  const ranges = marks
    .filter((m) => m.start >= 0 && m.end <= text.length && m.start < m.end)
    .sort((a, b) => a.start - b.start);
  const pieces: { text: string; marked: boolean }[] = [];
  let at = 0;
  for (const r of ranges) {
    if (r.start < at) continue;
    if (r.start > at) pieces.push({ text: text.slice(at, r.start), marked: false });
    pieces.push({ text: text.slice(r.start, r.end), marked: true });
    at = r.end;
  }
  if (at < text.length) pieces.push({ text: text.slice(at), marked: false });
  return (
    <div className="max-h-[28rem] overflow-y-auto font-serif text-[0.9375rem] leading-[1.7] whitespace-pre-line">
      {pieces.map((p, i) =>
        p.marked ? (
          <mark key={i} className="rounded-[2px] bg-mark box-decoration-clone text-inherit">
            {p.text}
          </mark>
        ) : (
          <span key={i}>{p.text}</span>
        ),
      )}
    </div>
  );
}

function OpenLink({ source, citation }: { source: ChatSource; citation?: ChatCitation }) {
  const className =
    "mt-2.5 inline-block text-xs underline decoration-line-strong underline-offset-4 hover:decoration-ink";
  if (source.source === "call" && source.call_id !== null) {
    const at = citation?.at ?? source.start ?? 0;
    return (
      <Link href={`/calls/${source.call_id}?t=${Math.floor(at)}`} className={className}>
        Open the transcript at {timestamp(at)}
      </Link>
    );
  }
  if (source.document_id !== null && source.section) {
    const p = citation?.paragraph_id ? `&p=${encodeURIComponent(citation.paragraph_id)}` : "";
    return (
      <Link href={`/documents/${source.document_id}?item=${source.section}${p}`} className={className}>
        Open Item {source.section} of the 10-K
      </Link>
    );
  }
  return null;
}
