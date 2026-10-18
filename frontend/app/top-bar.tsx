import Link from "next/link";
import type { ReactNode } from "react";

// The strip across the top of every page: where you are on the left, actions right.
export default function TopBar({ children, actions }: { children: ReactNode; actions?: ReactNode }) {
  return (
    <header className="sticky top-0 z-20 flex h-12 items-center gap-3 border-b border-line bg-sheet px-4 sm:px-5">
      <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-2 text-ink-2">
        {children}
      </nav>
      <div className="ml-auto flex shrink-0 items-center gap-3">{actions}</div>
    </header>
  );
}

export function Crumb({ href, children }: { href?: string; children: ReactNode }) {
  if (!href) return <span className="truncate font-medium text-ink">{children}</span>;
  return (
    <>
      <Link href={href} className="hover:text-ink">
        {children}
      </Link>
      <span aria-hidden className="text-line-strong">
        /
      </span>
    </>
  );
}

export function AnalyzeLink() {
  return (
    <Link
      href="/#analyze"
      className="inline-flex h-8 items-center rounded-md bg-ink px-3 font-medium text-sheet hover:bg-ink/85"
    >
      Analyze a filing
    </Link>
  );
}
