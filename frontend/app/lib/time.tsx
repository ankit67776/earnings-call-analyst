import { clock } from "./format";

// Server and browser can sit in different time zones; the browser's reading wins.
export function Clock({ iso }: { iso: string }) {
  return (
    <time dateTime={iso} suppressHydrationWarning>
      {clock(iso)}
    </time>
  );
}
