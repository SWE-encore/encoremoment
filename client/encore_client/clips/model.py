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
        gaps = ", ".join(f"{a:g}–{b:g}s" for a, b in clip.gaps)
        label += f" (missing: {gaps})"
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
