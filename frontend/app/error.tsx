"use client";

export default function ErrorPage({ retry }: { error: Error & { digest?: string }; retry: () => void }) {
  return (
    <main className="mx-auto max-w-3xl px-6 py-20">
      <h1 className="text-2xl font-semibold">Couldn&apos;t load the analysis</h1>
      <p className="mt-3 max-w-[60ch] leading-relaxed text-ink-soft">
        This page gets its data from the analysis service, which didn&apos;t respond. Start it
        with <code>uv run uvicorn app.main:app</code> in the <code>backend</code> folder, then
        try again.
      </p>
      <button
        type="button"
        onClick={() => retry()}
        className="mt-6 rounded-md bg-ink px-4 py-2 font-medium text-page hover:bg-ink/85"
      >
        Try again
      </button>
    </main>
  );
}
