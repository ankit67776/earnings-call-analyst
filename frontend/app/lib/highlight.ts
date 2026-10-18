import type { ClaimType, MatchKind } from "./types";

export interface Mark {
  findingId: number;
  type: ClaimType;
  match: MatchKind;
  start: number;
  end: number;
}

export interface Segment {
  text: string;
  mark: Mark | null;
}

/**
 * Split a paragraph into plain and highlighted runs. Where quotes overlap, the
 * selected finding wins, then the quote that started first.
 */
export function segments(text: string, marks: Mark[], selectedId: number | null): Segment[] {
  const valid = marks.filter((m) => m.start >= 0 && m.end <= text.length && m.start < m.end);
  if (valid.length === 0) return [{ text, mark: null }];

  const cuts = new Set([0, text.length]);
  for (const m of valid) {
    cuts.add(m.start);
    cuts.add(m.end);
  }
  const points = [...cuts].sort((a, b) => a - b);

  const out: Segment[] = [];
  for (let i = 0; i < points.length - 1; i++) {
    const [from, to] = [points[i], points[i + 1]];
    const covering = valid.filter((m) => m.start <= from && m.end >= to);
    const mark =
      covering.find((m) => m.findingId === selectedId) ??
      covering.sort((a, b) => a.start - b.start)[0] ??
      null;
    const last = out[out.length - 1];
    if (last && last.mark === mark) {
      last.text += text.slice(from, to);
    } else {
      out.push({ text: text.slice(from, to), mark });
    }
  }
  return out;
}
