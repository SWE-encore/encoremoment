// All times share the PC capture clock, including server trigger timestamps.
export const SEGMENT_MS = 5000, PRE_MS = 15000, POST_MS = 10000;
export const EXTEND_MS = 5000, LIMIT_MS = 60000;
// Keep another segment on each side for editing, plus two segments for late delivery.
export const HANDLE_MS = SEGMENT_MS, RETENTION_MS = PRE_MS + HANDLE_MS + 2 * SEGMENT_MS;

export class ClipPlan {
  constructor(eventId, reaction) {
    this.id = eventId;
    this.reaction = reaction;
    this.revision = 0;
    this.seen = new Set();
    this.parts = [];
  }

  apply(event) {
    if (event.revision <= this.revision || this.final) return false;
    this.revision = event.revision;
    for (const t of event.capture.triggers_ms) {
      if (this.seen.has(t)) continue;
      this.seen.add(t);
      let part = this.parts.at(-1);
      if (!part || t > part.end || part.end >= part.start + LIMIT_MS) {
        if (part) part.sealed = true;
        this.parts.push({start:t-PRE_MS, end:t+POST_MS, index:this.parts.length+1, sealed:false});
      } else {
        part.end = Math.min(part.end + EXTEND_MS, part.start + LIMIT_MS);
      }
    }
    if (event.phase === "FINAL") {
      this.final = true;
      if (this.parts.length) this.parts.at(-1).sealed = true;
    }
    return true;
  }
}

export function overlaps(segment, part) {
  return segment.start < part.end + HANDLE_MS && segment.end > part.start - HANDLE_MS;
}

export function missing(start, end, segments) {
  const gaps = [];
  let cursor = start;
  for (const seg of segments) {
    if (seg.end <= cursor || seg.start >= end) continue;
    if (seg.start > cursor) gaps.push([cursor, seg.start]);
    cursor = Math.max(cursor, seg.end);
  }
  if (cursor < end) gaps.push([cursor, end]);
  return gaps;
}
