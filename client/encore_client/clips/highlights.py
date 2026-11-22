"""Tonight's Highlights (R4, Jira EM-13). Qt-free, so it can be unit-tested.

Specification IV-D:

    events = final_undeleted_events(session)
    events.sort_by(-intensity, start, event_id)
    for e in events:
        clip = local_clip_index.lookup(e.id)
        render(e, clip.status, clip.path_if_ready)

Rules implemented here:

- Only FINAL events of this session are listed.
- Order is deterministic: observed intensity, highest first (events without an
  intensity last), then the earlier trigger, then event_id.
- Every event is listed, with or without footage. A play link is offered only
  for a saved clip whose file is finalized and checked (``playable_path``):
  pending, unavailable, failed, deletion-pending and missing files get none.
- Clips are matched to events only through the clip index's explicit
  ``session_id`` and ``event_ids``. Several events may share one clip.
- When no saved clip exists yet, the contract's clip plan
  (``SessionView.clips()``) explains what to expect, e.g. that the camera was
  off, without ever offering a link.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..session.session import event_rows
from .model import STATUS_LABEL, Clip, ClipStatus, effective_label, playable_path

NO_CLIP = "no exported clip linked"

PLAN_TEXT = {
    ("pending", None): "clip expected after the reaction ends",
    ("ready", None): "clip expected, not exported yet",
    ("partial", "before_camera_activation"): "partial clip expected (camera turned on late), not exported yet",
    ("unavailable", "camera_off"): "no footage: camera was off",
    ("unavailable", "camera_stopped"): "no footage: camera was stopped during the clip",
    ("unavailable", "unknown_camera_generation"): "no footage: camera state unknown",
}


@dataclass(frozen=True)
class ClipLink:
    """One clip of a highlight. ``path`` is set only when the file may be played."""
    name: str                  # "Clip", "Part 1", "Part 2 (continues part 1)"
    status: str                # text shown next to the clip
    path: Path | None = None
    clip_id: str | None = None
    shared_with: tuple[str, ...] = ()   # other events using the same clip


@dataclass(frozen=True)
class Highlight:
    rank: int
    event_id: str
    type: str
    intensity: float | None
    confidence: float | None
    trigger_ms: int
    title: str | None
    timecode_ms: int | None
    clips: tuple[ClipLink, ...]

    @property
    def playable(self) -> tuple[ClipLink, ...]:
        return tuple(c for c in self.clips if c.path is not None)

    @property
    def clip_summary(self) -> str:
        if not any(c.clip_id for c in self.clips):
            plans = "; ".join(c.status for c in self.clips)
            return f"{NO_CLIP} — {plans}" if plans else NO_CLIP
        return "; ".join(f"{c.name}: {c.status}" for c in self.clips)


def order_key(row: dict) -> tuple:
    """Highest intensity first, then earlier trigger, then event_id (deterministic ties)."""
    intensity = row["intensity"]
    return (intensity is None, -(intensity or 0.0), row["trigger_ms"], row["event_id"])


def build_highlights(view, clips: Iterable[Clip], base_dir: Path) -> list[Highlight]:
    """Highlights of one session from the received events, the clip plan and the clip index."""
    session_id = view.session_id
    saved: dict[str, list[Clip]] = {}
    for clip in clips:
        if clip.session_id == session_id:
            for event_id in clip.event_ids:
                saved.setdefault(event_id, []).append(clip)
    plans: dict[str, list[dict]] = {}
    for plan in view.clips():
        plans.setdefault(plan["event_id"], []).append(plan)

    rows = sorted((r for r in event_rows(view) if r["phase"] == "FINAL"), key=order_key)
    out = []
    for rank, row in enumerate(rows, start=1):
        event_id = row["event_id"]
        links = (_saved_links(event_id, saved[event_id], base_dir) if event_id in saved
                 else _planned_links(plans.get(event_id, [])))
        out.append(Highlight(rank, event_id, row["type"], row["intensity"], row["confidence"],
                             row["trigger_ms"], row["title"], row["timecode_ms"], links))
    return out


def _saved_links(event_id: str, clips: list[Clip], base_dir: Path) -> tuple[ClipLink, ...]:
    clips = sorted(clips, key=lambda c: (c.part, c.clip_id))
    links = []
    for clip in clips:
        name = "Clip" if len(clips) == 1 else f"Part {clip.part + 1}"
        if clip.status is ClipStatus.UNAVAILABLE:
            status = f"{STATUS_LABEL[clip.status]} (export failed or no footage)"
        else:
            status = effective_label(clip, base_dir)
        links.append(ClipLink(name, status, playable_path(clip, base_dir), clip.clip_id,
                              tuple(e for e in clip.event_ids if e != event_id)))
    return tuple(links)


def _planned_links(plans: list[dict]) -> tuple[ClipLink, ...]:
    links = []
    for plan in sorted(plans, key=lambda p: p["index"]):
        if len(plans) == 1:
            name = "Clip"
        elif plan["linked_to"] is not None:
            name = f"Part {plan['index'] + 1} (continues part {plan['linked_to'] + 1})"
        else:
            name = f"Part {plan['index'] + 1}"
        text = PLAN_TEXT.get((plan["expected_status"], plan["reason"]),
                             f"clip expected: {plan['expected_status']}")
        links.append(ClipLink(name, text if len(plans) == 1 else f"{name}: {text}"))
    return tuple(links)
