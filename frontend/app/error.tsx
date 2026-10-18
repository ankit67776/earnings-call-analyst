"use client";

import TopBar, { Crumb } from "./top-bar";

export default function ErrorPage({ retry }: { error: Error & { digest?: string }; retry: () => void }) {
  return (
    <>
      <TopBar>
        <Crumb>Something went wrong</Crumb>
      </TopBar>
      <main className="mx-auto max-w-[76rem] px-4 pt-7 sm:px-8">
        <h1 className="text-xl font-semibold tracking-[-0.015em]">Couldn&apos;t load this page</h1>
        <p className="mt-2 max-w-[65ch] leading-relaxed text-ink-2">
          Its data comes from the analysis service, which didn&apos;t answer. Start it with{" "}
          <code className="font-mono text-[0.8125rem] text-ink">uv run uvicorn app.main:app</code> in
          the <code className="font-mono text-[0.8125rem] text-ink">backend</code> folder, then try
          again.
        </p>
        <button
          type="button"
          onClick={() => retry()}
          className="mt-5 h-8 rounded-md bg-ink px-3 font-medium text-sheet hover:bg-ink/85"
        >
          Try again
        </button>
      </main>
    </>
  );
}
