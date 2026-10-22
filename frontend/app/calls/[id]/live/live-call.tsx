"use client";

import { memo, useEffect, useReducer, useRef, useState } from "react";

import { companyName, timestamp } from "@/app/lib/format";
import type { CallDetail, LiveEvent, LiveSession, LiveStatus, LiveWord } from "@/app/lib/types";
import TopBar, { Crumb } from "@/app/top-bar";

const PRIMARY =
  "inline-flex h-8 items-center rounded-md bg-ink px-3 font-medium text-sheet hover:bg-ink/85 disabled:cursor-progress disabled:opacity-60";
const SECONDARY =
  "inline-flex h-8 items-center rounded-md border border-line-strong bg-sheet px-3 font-medium hover:bg-canvas";

// How far the audio may wander from the session before it's moved back, in seconds.
const MAX_DRIFT = 0.75;
// Reconnections in a row before giving up on a session.
const MAX_RETRIES = 5;

interface Turn {
  speaker: number | null;
  words: LiveWord[];
}

interface State {
  session: LiveSession | null;
  turns: Turn[];
  // Words Deepgram may still revise, shown after the last settled ones.
  interim: LiveWord[];
}

// Session time `elapsed` at `at` on this page's clock, so the position can tick between
// events; stopped when the session ends.
interface Clock {
  elapsed: number;
  at: number;
  running: boolean;
}

// Finals never overlap, so their words go on the end; a new speaker starts a new turn.
function append(turns: Turn[], words: LiveWord[]): Turn[] {
  const out = turns.slice();
  for (const w of words) {
    const last = out.at(-1);
    if (last && last.speaker === w.speaker) out[out.length - 1] = { ...last, words: [...last.words, w] };
    else out.push({ speaker: w.speaker, words: [w] });
  }
  return out;
}

function reducer(state: State, event: LiveEvent): State {
  switch (event.type) {
    case "session":
      // Every connection opens with the session's state, then replays its history.
      return { session: event, turns: [], interim: [] };
    case "final":
      return { ...state, turns: append(state.turns, event.words), interim: [] };
    case "interim":
      return { ...state, interim: event.words };
    case "end":
      if (!state.session) return state;
      return { ...state, interim: [], session: { ...state.session, status: event.status, error: event.error } };
    default:
      return state;
  }
}

function positionAt(session: LiveSession, clock: Clock | null, now: number): number {
  if (!clock) return session.position;
  const elapsed = clock.running ? clock.elapsed + (now - clock.at) / 1000 : clock.elapsed;
  return session.offset + Math.min(Math.max(elapsed, 0) * session.speed, session.duration);
}

function speakerName(speaker: number | null): string {
  return speaker === null ? "Speaker" : `Speaker ${speaker + 1}`;
}

function text(words: LiveWord[]): string {
  return words.map((w) => w.text).join(" ");
}

// Re-renders every quarter second while `active`.
function useNow(active: boolean): number {
  const [now, setNow] = useState(0);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(performance.now()), 250);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}

async function failure(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
  } catch {}
  return `The backend returned ${response.status}.`;
}

const TurnView = memo(function TurnView({ turn, pending }: { turn: Turn; pending?: LiveWord[] }) {
  const first = turn.words[0] ?? pending?.[0];
  return (
    <section className="grid gap-x-6 gap-y-1 sm:grid-cols-[9rem_minmax(0,36rem)]">
      <header className="flex items-baseline gap-2 sm:flex-col sm:gap-0.5">
        <span className="font-medium">{speakerName(turn.speaker)}</span>
        {first && <span className="font-mono text-2xs text-ink-3 tabular-nums">{timestamp(first.start)}</span>}
      </header>
      <p className="font-serif text-base leading-[1.75]">
        {text(turn.words)}
        {pending && pending.length > 0 && <span className="text-ink-3"> {text(pending)}</span>}
      </p>
    </section>
  );
});

const STATUS: Record<LiveStatus, { label: string; dot: string }> = {
  live: { label: "Live", dot: "bg-accent pulse" },
  done: { label: "Ended", dot: "bg-ink-3" },
  stopped: { label: "Stopped", dot: "bg-ink-3" },
  failed: { label: "Failed", dot: "bg-bad" },
};

function Strip({
  session,
  clock,
  sound,
  onSound,
}: {
  session: LiveSession;
  clock: Clock | null;
  sound: boolean;
  onSound: () => void;
}) {
  const now = useNow(session.status === "live");
  const position = positionAt(session, clock, now);
  const end = session.offset + session.duration;
  const status = STATUS[session.status];
  return (
    <div className="sticky top-12 z-10 border-b border-line bg-sheet px-4 pt-2 sm:px-5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-ink-2">
        <span className="flex items-center gap-2">
          <span aria-hidden className={`inline-block size-[7px] rounded-full ${status.dot}`} />
          <span className="font-medium text-ink">{status.label}</span>
        </span>
        <span className="font-mono text-xs text-ink tabular-nums">
          {timestamp(position)} / {timestamp(end)}
        </span>
        <span>
          {session.provider === "deepgram" ? "Deepgram" : session.provider}{" "}
          <span className="font-mono text-xs text-ink">{session.model}</span>
          {session.recorded ? ", a recorded stream played on its original timing" : ", streaming now"}
        </span>
        {session.status === "failed" && session.error && <span className="text-bad">{session.error}</span>}
        {session.status === "live" && (
          <button
            type="button"
            onClick={onSound}
            aria-pressed={sound}
            className="ml-auto h-7 rounded-md px-2 font-medium text-ink hover:bg-canvas"
          >
            {sound ? "Sound off" : "Sound on"}
          </button>
        )}
      </div>
      <div className="mt-2 h-0.5 bg-line" aria-hidden>
        <div className="h-full bg-accent" style={{ width: `${(100 * (position - session.offset)) / session.duration}%` }} />
      </div>
    </div>
  );
}

export default function LiveCall({ call }: { call: CallDetail }) {
  const [state, dispatch] = useReducer(reducer, { session: null, turns: [], interim: [] });
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [clock, setClock] = useState<Clock | null>(null);
  const [sound, setSound] = useState(false);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [following, setFollowing] = useState(true);
  const audio = useRef<HTMLAudioElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const name = companyName(call.company);
  const { session, turns, interim } = state;
  const live = session?.status === "live";

  // Join the call's session if one is running (or show the last one's transcript).
  useEffect(() => {
    let cancelled = false;
    fetch(`/api/live/sessions?call_id=${call.id}`)
      .then((r) => (r.ok ? (r.json() as Promise<LiveSession[]>) : []))
      .then(([latest]) => {
        if (!cancelled && latest) setSessionId(latest.id);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [call.id]);

  // Follow the session's events; reconnect (and catch up from its history) if the
  // connection drops or the backend dropped this tab for falling behind.
  useEffect(() => {
    if (!sessionId || attempt > MAX_RETRIES) return;
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${scheme}://${location.host}/api/live/sessions/${sessionId}/events`);
    let retry: ReturnType<typeof setTimeout> | undefined;
    socket.onmessage = (message) => {
      const event = JSON.parse(message.data) as LiveEvent;
      if (event.type === "session") {
        setClock({ elapsed: event.elapsed, at: performance.now(), running: event.status === "live" });
        if (event.status === "live") setAttempt(0);
      } else if (event.type === "end") {
        setClock({ elapsed: event.published, at: performance.now(), running: false });
      }
      dispatch(event);
    };
    socket.onclose = (event) => {
      if (event.code !== 1000 && event.code !== 4404) {
        retry = setTimeout(() => setAttempt((n) => n + 1), 1000);
      }
    };
    return () => {
      socket.onclose = null;
      socket.close();
      clearTimeout(retry);
    };
  }, [sessionId, attempt]);

  // Keep the audio where the session is.
  useEffect(() => {
    const player = audio.current;
    if (!player || !session) return;
    if (session.status !== "live") {
      if (session.status !== "done") player.pause();
      return;
    }
    if (!sound) return;
    const timer = setInterval(() => {
      const target = positionAt(session, clock, performance.now());
      if (Math.abs(player.currentTime - target) > MAX_DRIFT) player.currentTime = target;
    }, 1000);
    return () => clearInterval(timer);
  }, [session, clock, sound]);

  // Follow the newest words unless the reader has scrolled up.
  useEffect(() => {
    const target = bottom.current;
    if (!target) return;
    const observer = new IntersectionObserver(([entry]) => setFollowing(entry.isIntersecting));
    observer.observe(target);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (following && live) bottom.current?.scrollIntoView({ block: "end" });
  }, [turns, interim, following, live]);

  async function start() {
    setStarting(true);
    setError(null);
    const player = audio.current;
    // Sound has to start inside the click or the browser blocks it; the sync above
    // then moves it to wherever the session is.
    if (player) {
      player.currentTime = 0;
      player.play().then(
        () => setSound(true),
        () => setSound(false),
      );
    }
    try {
      const response = await fetch("/api/live/sessions", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ call_id: call.id }),
      });
      if (!response.ok) throw new Error(await failure(response));
      const started = (await response.json()) as LiveSession;
      setAttempt(0);
      setSessionId(started.id);
    } catch (e) {
      player?.pause();
      setSound(false);
      setError(e instanceof Error ? e.message : "Couldn't start the call.");
    } finally {
      setStarting(false);
    }
  }

  async function stop() {
    if (!session) return;
    audio.current?.pause();
    setSound(false);
    await fetch(`/api/live/sessions/${session.id}`, { method: "DELETE" }).catch(() => {});
  }

  function toggleSound() {
    const player = audio.current;
    if (!player || !session) return;
    if (sound) {
      player.pause();
      setSound(false);
      return;
    }
    player.currentTime = positionAt(session, clock, performance.now());
    player.play().then(
      () => setSound(true),
      () => setError("The browser blocked the sound. Try again."),
    );
  }

  // Interim words carry on the last turn when they're the same speaker's.
  const last = turns.at(-1);
  const pendingOwnTurn = interim.length > 0 && (!last || last.speaker !== interim[0].speaker);

  return (
    <>
      <TopBar
        actions={
          live ? (
            <button type="button" onClick={stop} className={SECONDARY}>
              Stop
            </button>
          ) : session ? (
            <button type="button" onClick={start} disabled={starting} className={PRIMARY}>
              Play again
            </button>
          ) : null
        }
      >
        <Crumb href="/">Library</Crumb>
        <Crumb href={`/calls/${call.id}`}>
          {name} <span className="font-mono text-xs text-ink-2">Q{call.quarter} {call.fiscal_year} call</span>
        </Crumb>
        <Crumb>Live</Crumb>
      </TopBar>

      {session && <Strip session={session} clock={clock} sound={sound} onSound={toggleSound} />}

      <div className="mx-auto max-w-[84rem] px-4 py-6 sm:px-8">
        <article className="rounded-lg border border-line bg-sheet px-4 pt-9 pb-16 sm:px-8">
          <h1 className="font-serif text-[1.75rem] leading-tight font-medium tracking-[-0.01em]">
            {name}, Q{call.quarter} {call.fiscal_year} earnings call
          </h1>
          {session ? (
            <p className="mt-2 max-w-[70ch] leading-relaxed text-ink-2">
              Speakers are numbered by Deepgram as it hears them, and one number can cover more than one
              person. Grey words are its first guess and may still change.
            </p>
          ) : (
            <>
              <p className="mt-2 max-w-[62ch] leading-[1.625] text-ink-2">
                Plays the call as if it were happening now: its audio streams to Deepgram in real time, and the
                transcript appears as Deepgram hears it, about a second behind the voice.
              </p>
              <button type="button" onClick={start} disabled={starting} className={`mt-5 ${PRIMARY}`}>
                {starting ? "Starting…" : "Start the call"}
              </button>
            </>
          )}
          {error && (
            <p role="status" className="mt-3 text-bad">
              {error}
            </p>
          )}

          <div className="mt-8 space-y-5">
            {turns.map((turn, i) => (
              <TurnView
                key={turn.words[0].start}
                turn={turn}
                pending={i === turns.length - 1 && !pendingOwnTurn ? interim : undefined}
              />
            ))}
            {pendingOwnTurn && <TurnView turn={{ speaker: interim[0].speaker, words: [] }} pending={interim} />}
          </div>
          <div ref={bottom} className="h-px" />
        </article>
      </div>

      {live && !following && (
        <button
          type="button"
          onClick={() => bottom.current?.scrollIntoView({ block: "end", behavior: "smooth" })}
          className={`fixed bottom-6 left-1/2 z-20 -translate-x-1/2 shadow-[0_12px_32px_-12px_rgb(18_22_27/0.25)] lg:left-[calc(50%+7rem)] ${SECONDARY}`}
        >
          Jump to live
        </button>
      )}

      <audio ref={audio} preload="metadata" src={`/api/calls/${call.id}/audio`} />
    </>
  );
}
