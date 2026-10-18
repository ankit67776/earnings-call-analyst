import Link from "next/link";

import TopBar, { Crumb } from "./top-bar";

export default function NotFound() {
  return (
    <>
      <TopBar>
        <Crumb href="/">Library</Crumb>
        <Crumb>Not found</Crumb>
      </TopBar>
      <main className="mx-auto max-w-[76rem] px-4 pt-7 sm:px-8">
        <h1 className="text-xl font-semibold tracking-[-0.015em]">No filing here</h1>
        <p className="mt-2 max-w-[65ch] leading-relaxed text-ink-2">
          There&apos;s no analyzed filing at this address. It may have been removed, or the link
          is mistyped.
        </p>
        <Link
          href="/"
          className="mt-5 inline-flex h-8 items-center rounded-md border border-line-strong bg-sheet px-3 font-medium hover:bg-canvas"
        >
          Back to the library
        </Link>
      </main>
    </>
  );
}
