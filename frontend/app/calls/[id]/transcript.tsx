"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef } from "react";

import { companyName, plural, timestamp } from "@/app/lib/format";
import type { CallChecks, CallDetail, CallSegment } from "@/app/lib/types";
import TopBar, { Crumb } from "@/app/top-bar";
import Checks from "./checks";

interface Turn {
  speaker: string;
  segments: CallSegment[];
}

function turns(segments: CallSegment[]): Turn[] {
  const out: Turn[] = [];
  for (const s of segments) {
    const last = out[out.length - 1];
    if (last && last.segments[0].turn === s.turn) last.segments.push(s);
    else out.push({ speaker: s.speaker, segments: [s] });
  }
  return out;
}

// The segment playing at `at` seconds, or the next one to start.
function segmentAt(segments: CallSegment[], at: number): number | null {
  const hit = segments.find((s) => s.start <= at && at < s.end) ?? segments.find((s) => s.start >= at);
  return hit?.position ?? null;
}

export default function Transcript({
  call,
  checks,
  at,
}: {
  call: CallDetail;
  checks: CallChecks | null;
  at: number | null;
}) {
  const runs = useMemo(() => turns(call.segments), [call.segments]);
  const speakers = [...new Set(call.segments.map((s) => s.speaker).filter(Boolean))];
  const target = at === null ? null : segmentAt(call.segments, at);
  const targetRef = useRef<HTMLSpanElement>(null);
  const name = companyName(call.company);

  useEffect(() => {
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    targetRef.current?.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
  }, []);

  return (
    <>
      <TopBar
        actions={
          <>
            <Link
              href={`/calls/${call.id}/live`}
              className="inline-flex h-8 items-center rounded-md border border-line-strong bg-sheet px-3 font-medium hover:bg-canvas"
            >
              Play live
            </Link>
            <Link
              href={`/ask?ticker=${call.ticker}`}
              className="inline-flex h-8 items-center rounded-md bg-ink px-3 font-medium text-sheet hover:bg-ink/85"
            >
              Ask about {name}
            </Link>
          </>
        }
      >
        <Crumb href="/">Library</Crumb>
        <Crumb>
          {name} <span className="font-mono text-xs text-ink-2">Q{call.quarter} {call.fiscal_year} call</span>
        </Crumb>
      </TopBar>

      <div className="border-b border-line bg-sheet px-4 py-2.5 text-ink-2 sm:px-5">
        <p className="leading-relaxed">
          <span className="tabular-nums">{Math.round(call.duration / 60)} min</span> ·{" "}
          {plural(runs.length, "speaker turn")} · transcribed by{" "}
          <span className="font-mono text-xs text-ink">{call.stt_model}</span> on {call.stt_provider}
          {call.speaker_source && <> · speakers from the {call.speaker_source}</>}
        </p>
      </div>

      <div className="mx-auto max-w-[84rem] px-4 py-6 sm:px-8">
        <article className="rounded-lg border border-line bg-sheet px-4 pt-9 pb-16 sm:px-8">
          <h1 className="font-serif text-[1.75rem] leading-tight font-medium tracking-[-0.01em]">
            {name}, Q{call.quarter} {call.fiscal_year} earnings call
          </h1>
          {speakers.length > 0 && (
            <p className="mt-2 max-w-[70ch] leading-relaxed text-ink-2">
              {plural(speakers.length, "speaker")}: {speakers.join(", ")}
            </p>
          )}
          {checks && <Checks checks={checks} />}
          <div className="mt-8 space-y-5">
            {runs.map((turn) => (
              <section
                key={turn.segments[0].position}
                className="grid gap-x-6 gap-y-1 sm:grid-cols-[9rem_minmax(0,36rem)]"
              >
                <header className="flex items-baseline gap-2 sm:flex-col sm:gap-0.5">
                  <span className="font-medium">{turn.speaker || "Unknown speaker"}</span>
                  <a
                    href={`?t=${Math.floor(turn.segments[0].start)}`}
                    className="font-mono text-2xs text-ink-3 tabular-nums hover:text-ink"
                  >
                    {timestamp(turn.segments[0].start)}
                  </a>
                </header>
                <p className="font-serif text-base leading-[1.75]">
                  {turn.segments.map((s, i) => (
                    <span
                      key={s.position}
                      ref={s.position === target ? targetRef : undefined}
                      data-t={s.start}
                      className={
                        s.position === target ? "rounded-[2px] bg-mark-soft box-decoration-clone" : undefined
                      }
                    >
                      {i > 0 && " "}
                      {s.text}
                    </span>
                  ))}
                </p>
              </section>
            ))}
          </div>
        </article>
      </div>
    </>
  );
}
