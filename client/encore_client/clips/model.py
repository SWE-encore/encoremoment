"""Clip list model (Qt-free, so it can be unit-tested).

Rules (Specification IV-D, Requirement 4):
  - Clip status: pending / ready / partial / unavailable / deletion_pending / deleted
  - A playback link is shown only for a saved file that has been finalized and checked.
    -> Playable only if the status is ready or partial, the file exists, and it is not empty.
  - If the index says "ready" but the file is missing, hide the playback link and show "File missing".
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ClipStatus(str, Enum):
    PENDING = "pending"                    # Being created (waiting for post-context or FFmpeg assembly)
    READY = "ready"                        # Finalized and checked
    PARTIAL = "partial"                    # Finalized, but some portions are missing (e.g. camera enabled late)
    UNAVAILABLE = "unavailable"            # No footage (camera off, export failed) -> metadata only
    DELETION_PENDING = "deletion_pending"  # Deletion requested, cleanup not finished (R7)
    DELETED = "deleted"                    # Media removed; event metadata may remain (R7)


STATUS_LABEL = {
    ClipStatus.PENDING: "⏳ Processing",
    ClipStatus.READY: "✅ Ready",
    ClipStatus.PARTIAL: "⚠️ Partial",
    ClipStatus.UNAVAILABLE: "— No footage",
    ClipStatus.DELETION_PENDING: "🗑 Deleting",
    ClipStatus.DELETED: "🗑 Deleted",
}

PLAYABLE_STATUSES = {ClipStatus.READY, ClipStatus.PARTIAL}

# Why a clip has no footage or only part of it. One table for every view (Highlights,
# Saved clips, clip plans), keyed by the codes the recorder, crash recovery and the
# contract's clip plan write to the ``reason`` field.
REASON_TEXT = {
    "camera_off": "camera was off",
    "camera_stopped": "camera was stopped during the clip",
    "unknown_camera_generation": "camera state unknown",
    "before_camera_activation": "camera turned on late",
    "no_footage": "no footage was recorded",
    "too_many_pending_clips": "not recorded: too many clips were waiting",
    "interrupted_capture": "recording stopped when the app closed",
}
NO_REASON = "export failed or no footage"
MAX_REASON_CHARS = 80   # free-text export errors are shortened for one-line labels
LISTED_GAPS = 3         # partial labels list at most this many missing ranges


def reason_text(reason: str | None) -> str:
    """Readable cause for a clip without (full) footage. Unknown codes are export errors."""
    if not reason:
        return NO_REASON
    if reason in REASON_TEXT:
        return REASON_TEXT[reason]
    error = " ".join(reason.split())
    if len(error) > MAX_REASON_CHARS:
        error = error[:MAX_REASON_CHARS - 1].rstrip() + "…"
    return f"export failed: {error}"


def _range(a: float, b: float) -> str:
    return f"{a:g}–{b:g}s"


def gap_summary(gaps) -> str:
    """Short text for the missing ranges of a partial clip; stays one line for any count."""
    if len(gaps) <= LISTED_GAPS:
        return "missing: " + ", ".join(_range(a, b) for a, b in gaps)
    total = sum(max(0.0, b - a) for a, b in gaps)
    return f"missing {total:.1f} s in {len(gaps)} places"


def gap_detail(gaps, limit: int = 10) -> str:
    """Every missing range for a detail view, capped so a long list stays readable."""
    shown = ", ".join(_range(a, b) for a, b in gaps[:limit])
    more = len(gaps) - limit
    return f"Missing ranges: {shown}" + (f" … and {more} more" if more > 0 else "")


@dataclass
class Clip:
    clip_id: str
    event_type: str            # laughter / scream / gasp
    timecode: str              # Content timecode (e.g. "00:42:13")
    intensity: float
    status: ClipStatus
    path: str | None = None    # Path relative to the index file
    gaps: list[list[float]] = field(default_factory=list)  # Missing ranges [start_s, end_s]
    title: str = ""
    # Explicit link to live events (R4). The clip pipeline writes these; a clip
    # without them is never matched to an event by type or time.
    session_id: str | None = None
    event_ids: tuple[str, ...] = ()   # several events may share one clip
    part: int = 0                     # clip index within the event (R10 linked clips)
    # Other local files removed together with the clip (R7)
    thumbnail: str | None = None
    temp_paths: tuple[str, ...] = ()

    trim_ms: tuple[int, ...] = ()  # Default playback excludes the retained editing handles.
    reason: str | None = None      # REASON_TEXT code, or an export error, for unavailable/partial

    @classmethod
    def from_dict(cls, d: dict) -> "Clip":
        if not isinstance(d, dict):
            raise ValueError("clip index entry must be an object")
        for key in ("path", "thumbnail"):
            if d.get(key) is not None and not isinstance(d[key], str):
                raise ValueError(f"clip {key} must be a file name")
        temp_paths = d.get("temp_paths", [])
        if not isinstance(temp_paths, list) or any(not isinstance(path, str) for path in temp_paths):
            raise ValueError("clip temp_paths must be a list of file names")
        return cls(
            clip_id=d["clip_id"],
            event_type=d["event_type"],
            timecode=d["timecode"],
            intensity=float(d["intensity"]),
            status=ClipStatus(d["status"]),
            path=d.get("path"),
            gaps=d.get("gaps", []),
            title=d.get("title", ""),
            session_id=d.get("session_id"),
            event_ids=tuple(d.get("event_ids", ())),
            part=int(d.get("part", 0)),
            thumbnail=d.get("thumbnail"),
            temp_paths=tuple(temp_paths),
            trim_ms=tuple(d.get("trim_ms", ())),
            reason=d["reason"] if isinstance(d.get("reason"), str) else None,
        )


def playable_path(clip: Clip, base_dir: Path) -> Path | None:
    """Return the file path if the clip may be played; otherwise None (= do not show a playback link)."""
    if clip.status not in PLAYABLE_STATUSES or not clip.path:
        return None
    p = (base_dir / clip.path).resolve()
    try:
        if p.is_file() and p.stat().st_size > 0:
            return p
    except OSError:
        pass
    return None


def effective_label(clip: Clip, base_dir: Path) -> str:
    """Status text for the UI. If the index and the actual file disagree, the actual state wins."""
    if clip.status in PLAYABLE_STATUSES and playable_path(clip, base_dir) is None:
        return "❌ File missing"
    label = STATUS_LABEL[clip.status]
    if clip.status == ClipStatus.PARTIAL and clip.gaps:
        label += f" ({gap_summary(clip.gaps)})"
    elif clip.status == ClipStatus.UNAVAILABLE:
        label += f" ({reason_text(clip.reason)})"
    return label


def load_index(index_path: Path, *, required: bool = False) -> list[Clip]:
    """Read the local clip index (JSON). To be replaced by the SQLite index (Specification III-B)."""
    if not required and not index_path.is_file():
        return []
    data = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("clip index must be an object")
    entries = data["clips"] if required else data.get("clips", [])
    if not isinstance(entries, list):
        raise ValueError("clip index entries must be a list")
    return [Clip.from_dict(d) for d in entries]
