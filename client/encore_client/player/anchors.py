"""Playback anchors: the player's side of the encore.v1 contract.

An anchor says "at capture time t0 the player was at position p0, playing at
rate r" (or paused). Within one playback epoch the content timecode of any
capture time t is p0 + r * (t - t0) while playing, and p0 while paused
(Submission B, Requirement 3). The epoch changes on seek and on new content.

This module has no GStreamer or Qt dependency so it can be unit-tested and
reused by the session code (EM-23).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..clock import monotonic_ms

SCHEMA = "encore.v1"
_ID_SAFE = re.compile(r"[^A-Za-z0-9_.:-]")


@dataclass(frozen=True)
class Anchor:
    capture_ms: int
    epoch: int
    content_id: str
    title: str
    position_ms: int
    rate: float
    state: str  # "playing" | "paused"

    def timecode_at(self, capture_ms: int) -> int:
        if self.state != "playing":
            return self.position_ms
        return max(0, round(self.position_ms + self.rate * (capture_ms - self.capture_ms)))


def content_identity(path: str | Path) -> tuple[str, str]:
    """A stable content_id and a display title for a local file."""
    p = Path(path).resolve()
    digest = hashlib.sha1(str(p).encode()).hexdigest()[:16]
    return f"file:{digest}", p.stem or p.name


class AnchorTracker:
    """Numbers epochs and anchors and turns them into playback_anchor messages."""

    def __init__(
        self,
        session_id: str,
        emit: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], int] = monotonic_ms,
    ) -> None:
        self.session_id = session_id
        self.emit = emit or (lambda msg: None)
        self.clock = clock
        self.seq = 0
        self.epoch = 0
        self.content_id: str | None = None
        self.title: str | None = None
        self.history: list[Anchor] = []

    @property
    def last(self) -> Anchor | None:
        return self.history[-1] if self.history else None

    def new_content(self, content_id: str, title: str) -> None:
        """A different file was opened: start a new epoch."""
        if not content_id or len(content_id) > 128 or _ID_SAFE.search(content_id):
            raise ValueError(f"content_id must be 1-128 chars of [A-Za-z0-9_.:-]: {content_id!r}")
        if not title:
            raise ValueError("title must not be empty")
        self.content_id, self.title = content_id, title[:300]
        self.epoch += 1

    def seeked(self) -> None:
        """Playback jumped: positions before and after are not continuous."""
        self.epoch += 1

    def report(
        self, position_ms: int, rate: float, playing: bool, capture_ms: int | None = None
    ) -> dict[str, Any]:
        """Record where the player is now and emit a playback_anchor message."""
        if self.content_id is None or self.title is None:
            raise RuntimeError("report() before new_content()")
        if rate <= 0:
            raise ValueError("rate must be positive")
        anchor = Anchor(
            capture_ms=self.clock() if capture_ms is None else capture_ms,
            epoch=self.epoch,
            content_id=self.content_id,
            title=self.title,
            position_ms=max(0, int(position_ms)),
            rate=float(rate),
            state="playing" if playing else "paused",
        )
        self.history.append(anchor)
        msg = {
            "schema": SCHEMA,
            "kind": "playback_anchor",
            "session_id": self.session_id,
            "seq": self.seq,
            "capture_ms": anchor.capture_ms,
            "playback": {
                "epoch": anchor.epoch,
                "content_id": anchor.content_id,
                "title": anchor.title,
                "position_ms": anchor.position_ms,
                "rate": anchor.rate,
                "state": anchor.state,
            },
        }
        self.seq += 1
        self.emit(msg)
        return msg

    def anchor_for(self, capture_ms: int) -> Anchor | None:
        """The latest anchor at or before capture_ms, if any."""
        for anchor in reversed(self.history):
            if anchor.capture_ms <= capture_ms:
                return anchor
        return None

    def map_time(self, capture_ms: int) -> tuple[int, int] | None:
        """(epoch, content timecode in ms) for a capture time, or None if unknown.

        None means "pending" in the contract: no anchor existed yet, so the
        timecode must not be guessed.
        """
        anchor = self.anchor_for(capture_ms)
        if anchor is None:
            return None
        return anchor.epoch, anchor.timecode_at(capture_ms)
