import Link from "next/link";

export default function NotFound() {
  return (
    <main className="mx-auto max-w-3xl px-6 py-20">
      <h1 className="text-2xl font-semibold">No filing here</h1>
      <p className="mt-3 text-ink-soft">
        There&apos;s no analyzed filing at this address. It may have been removed, or the link
        is mistyped.
      </p>
      <Link href="/" className="mt-6 inline-block font-medium underline underline-offset-4">
        See all filings
      </Link>
    </main>
  );
}
