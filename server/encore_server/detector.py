"""Reaction events from per-frame model scores (R2, R3, R10).

For each analysis frame the model gives a confidence per reaction class.
This module decides when a reaction starts, fires again and ends, and builds
the contract's ``event`` objects (contracts/README.md):

- **OPEN** when a class's confidence reaches its trigger level. The trigger
  time t0 is the frame's centre; the reaction interval starts at the frame's
  start.
- **UPDATE** on a re-fire: confidence at or above the trigger level at least
  ``refire_ms`` after the previous trigger. The trigger is appended, which
  extends the clip by 5 s (R10), so continuous laughter keeps re-firing.
- **FINAL** once confidence has stayed below the release level (a fraction
  of the trigger level, for hysteresis) for ``hangover_ms``. ``end_ms`` is
  the end of the last frame at or above the release level.

Confidence is the model's confidence for the class, not intensity (R3).
Intensity is how loud the reaction was against the room (``RoomBaseline``).
The thresholds are versioned configuration (``DetectorConfig.name``): first
guesses, to be tuned on recordings of real audiences.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from .fusion import RollingFusion, TabRules

REACTIONS = ("laughter", "scream", "gasp")


@dataclass
class DetectorConfig:
    name: str = "detector-v1"
    trigger: dict[str, float] = field(
        default_factory=lambda: {"laughter": 0.4, "scream": 0.4, "gasp": 0.3}
    )
    release_ratio: float = 0.6
    refire_ms: int = 2_000
    hangover_ms: int = 1_500


@dataclass
class Frame:
    start_ms: int
    end_ms: int
    scores: dict[str, float]
    intensity: float  # 0..1 against the room baseline
    microphone: int  # microphone generation of the audio
    camera: int | None  # camera generation in effect, None while the camera is off
    aec: str
    visual: float | None = None


@dataclass
class _Open:
    event: dict[str, Any]
    last_active_ms: int
    aec: str


class ReactionDetector:
    def __init__(self, config: DetectorConfig | None = None, tab_rules: TabRules | None = None) -> None:
        self.config = config or DetectorConfig()
        self.tab_rules = tab_rules
        self.fusion = RollingFusion(tab_rules) if tab_rules else None
        self.open: dict[str, _Open] = {}
        self._count = 0

    def push(self, frame: Frame) -> list[tuple[dict[str, Any], str]]:
        """Feed one frame. Returns the events that changed, each with its AEC status."""
        cfg = self.config
        rules = self.tab_rules
        t = (frame.start_ms + frame.end_ms) // 2
        changes = []
        for reaction in REACTIONS:
            raw = min(max(float(frame.scores.get(reaction, 0.0)), 0.0), 1.0)
            score = round(raw, 3)
            level = cfg.trigger[reaction]
            if rules and reaction == "laughter":
                trigger = self.fusion.push(t, raw, frame.visual)
                hold = rules.holds(raw, frame.visual)
            else:
                trigger = score >= level
                hold = score >= level * cfg.release_ratio
            cur = self.open.get(reaction)
            if cur is None:
                if trigger:
                    changes.append(self._open(reaction, frame, t, score))
                continue
            ev = cur.event
            if hold:
                cur.last_active_ms = frame.end_ms
                ev["reaction"]["confidence"] = max(ev["reaction"]["confidence"], score)
                strength = max(ev["intensity"]["audio"], frame.intensity)
                visual = ev["intensity"]["visual"]
                if frame.visual is not None:
                    visual = max(visual or 0.0, frame.visual)
                ev["intensity"].update(combined=max(strength, visual or 0.0), audio=strength, visual=visual)
                if trigger and t - ev["capture"]["triggers_ms"][-1] >= cfg.refire_ms:
                    ev["capture"]["triggers_ms"].append(t)
                    ev["revision"] += 1
                    ev["phase"] = "UPDATE"
                    changes.append((copy.deepcopy(ev), cur.aec))
            elif frame.end_ms - cur.last_active_ms >= (rules.quiet_ms if rules and reaction == "laughter" else cfg.hangover_ms):
                changes.append(self._final(reaction))
        return changes

    def close_all(self) -> list[tuple[dict[str, Any], str]]:
        """FINAL for every open event: microphone off, stream ended, session end."""
        return [self._final(reaction) for reaction in list(self.open)]

    def _open(self, reaction: str, frame: Frame, t: int, score: float):
        self._count += 1
        ev = {
            "event_id": f"evt-{self._count:04d}",
            "revision": 1,
            "phase": "OPEN",
            "reaction": {"type": reaction, "confidence": score},
            "intensity": {"combined": max(frame.intensity, frame.visual or 0.0), "audio": frame.intensity, "visual": frame.visual},
            "capture": {"triggers_ms": [t], "start_ms": frame.start_ms, "end_ms": None},
            "generations": {"microphone": frame.microphone, "camera": frame.camera},
        }
        self.open[reaction] = _Open(ev, frame.end_ms, frame.aec)
        return copy.deepcopy(ev), frame.aec

    def _final(self, reaction: str):
        if self.fusion and reaction == "laughter":
            self.fusion.reset()
        cur = self.open.pop(reaction)
        ev = cur.event
        ev["revision"] += 1
        ev["phase"] = "FINAL"
        ev["capture"]["end_ms"] = cur.last_active_ms
        return copy.deepcopy(ev), cur.aec
