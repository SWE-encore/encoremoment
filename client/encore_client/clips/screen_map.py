"""Where a clip's screen recording is at each moment of its camera file.

A clip file holds the capture times in its row's ``coverage_ms`` back to back,
and its screen file (clips/screen.py) those in ``screen.coverage_ms``; matching
the two by capture time gives ``ScreenSegment``s. No Qt or GStreamer here, so
the clip model can use it.
"""
from __future__ import annotations

from dataclasses import dataclass

# The camera file keeps the time between its frames, so the small holes between
# frames in its coverage (EM-19) are not holes in the file; a missing camera
# fragment (one second) is.
CAMERA_HOLE_MS = 900


@dataclass(frozen=True)
class ScreenSegment:
    media_ms: tuple[int, int]   # part of the camera clip file
    path: str                   # the screen file, relative to the clip index
    screen_ms: int              # screen file position at media_ms[0]

    def screen_at(self, media_ms: int) -> int:
        return max(0, self.screen_ms + media_ms - self.media_ms[0])


def _offsets(coverage, *, join_below_ms=0):
    """(file position, start, end) of each part of a file holding ``coverage``
    back to back. Holes shorter than ``join_below_ms`` are kept in the file."""
    parts = []
    for start, end in coverage:
        start, end = int(start), int(end)
        if parts and start - parts[-1][1] < join_below_ms:
            parts[-1][1] = max(parts[-1][1], end)
        else:
            parts.append([start, end])
    media = 0
    for start, end in parts:
        yield media, start, end
        media += end - start


def screen_segments(row: dict) -> tuple[ScreenSegment, ...] | None:
    """Where the screen file is at each moment of the camera clip file, from the
    row's ``coverage_ms`` and ``screen``; None if the clip has no screen file."""
    screen = row.get("screen")
    try:
        if not isinstance(screen, dict) or not isinstance(screen.get("path"), str):
            return None
        segments = []
        for camera_at, a, b in _offsets(row["coverage_ms"], join_below_ms=CAMERA_HOLE_MS):
            for screen_at, c, d in _offsets(screen["coverage_ms"]):
                lo, hi = max(a, c), min(b, d)
                if lo < hi:
                    segments.append(ScreenSegment((camera_at + lo - a, camera_at + hi - a),
                                                  screen["path"], screen_at + lo - c))
        return tuple(sorted(segments, key=lambda s: s.media_ms))
    except (KeyError, TypeError, ValueError):
        return None


def segment_at(segments, media_ms: int, *, tail_ms: int = 1_000) -> ScreenSegment | None:
    """The segment at ``media_ms`` of the camera clip. Just past the last segment
    (the file's last frame) still counts as the last one."""
    for segment in segments:
        if segment.media_ms[0] <= media_ms < segment.media_ms[1]:
            return segment
    if segments and 0 <= media_ms - segments[-1].media_ms[1] < tail_ms:
        return segments[-1]
    return None
