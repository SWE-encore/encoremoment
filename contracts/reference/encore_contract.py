"""Reference receiver for the encore.v1 event/control contract (EM-1).

This is the PC-side view of one session: it applies the messages the PC sends
and receives, in the order it sees them, and derives the event list and the
clip windows the PC should cut. It is deliberately small and stdlib-only so the
PC client (EM-23/EM-24), the clip pipeline (EM-2) and the server can all test
against the same rules. It does no I/O and does not touch media files.

Rules implemented here are written out in contracts/README.md.
"""

from __future__ import annotations

import json
import struct
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# R10 clipping rule, in milliseconds.
PRE_MS = 15_000  # P: context before the trigger
POST_MS = 10_000  # Q: context after the trigger
EXTEND_MS = 5_000  # E: extension per re-fire
MAX_MS = 60_000  # L: maximum clip length

PC_TO_SERVER = {"playback_anchor", "control_update", "audio_chunk"}
SERVER_TO_PC = {"event_update", "control_ack"}
IMMUTABLE_EVENT_FIELDS = (("reaction", "type"), ("generations",), ("capture", "start_ms"))

# Audio upload: one binary frame = 4-byte big-endian header length, the UTF-8
# JSON audio_chunk header, then PCM signed 16-bit little-endian mono samples.
AUDIO_RATE = 16_000
AUDIO_MAX_SAMPLES = 16_000  # 1 second per chunk
_AUDIO_MAX_HEADER = 4_096
_LEN = struct.Struct("!I")


def encode_audio_frame(header: dict[str, Any], pcm: bytes) -> bytes:
    """Build the binary WebSocket payload for one audio chunk."""
    if header.get("kind") != "audio_chunk":
        raise ValueError("not an audio_chunk header")
    if len(pcm) != 2 * header.get("samples", -1) or not 0 < len(pcm) <= 2 * AUDIO_MAX_SAMPLES:
        raise ValueError(f"payload is {len(pcm)} bytes; header says {header.get('samples')} samples")
    head = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode()
    if len(head) > _AUDIO_MAX_HEADER:
        raise ValueError("audio_chunk header too large")
    return _LEN.pack(len(head)) + head + pcm


def decode_audio_frame(data: bytes) -> tuple[dict[str, Any], bytes]:
    """Split a binary frame into (header, pcm). Raises ValueError if malformed."""
    if len(data) < _LEN.size:
        raise ValueError("frame too short")
    (n,) = _LEN.unpack_from(data)
    if not 0 < n <= _AUDIO_MAX_HEADER or len(data) < _LEN.size + n:
        raise ValueError("bad header length")
    try:
        header = json.loads(data[_LEN.size : _LEN.size + n])
    except ValueError as exc:
        raise ValueError(f"header is not JSON: {exc}") from None
    if not isinstance(header, dict) or header.get("kind") != "audio_chunk":
        raise ValueError("binary frame without an audio_chunk header")
    pcm = data[_LEN.size + n :]
    if not isinstance(header.get("samples"), int) or len(pcm) != 2 * header["samples"]:
        raise ValueError(f"payload is {len(pcm)} bytes; header says {header.get('samples')} samples")
    return header, pcm


@dataclass
class ClipWindow:
    start_ms: int
    end_ms: int
    linked_to: int | None = None  # index of the clip this one continues

    @property
    def length_ms(self) -> int:
        return self.end_ms - self.start_ms


def clip_windows(triggers_ms: list[int]) -> list[ClipWindow]:
    """Apply the R10 clipping rule to an event's trigger times.

    - First trigger t0 gives [t0 - P, t0 + Q].
    - A re-fire at t <= current end extends the end by E, capped at start + L.
    - A re-fire at t <= end when the clip is already L long starts a new clip
      [t - P, t + Q] linked to the previous one.
    - A re-fire after the current end starts a new, unlinked clip.
    """
    windows: list[ClipWindow] = []
    for t in triggers_ms:
        cur = windows[-1] if windows else None
        if cur is None or t > cur.end_ms:
            windows.append(ClipWindow(t - PRE_MS, t + POST_MS))
        elif cur.length_ms >= MAX_MS:
            windows.append(ClipWindow(t - PRE_MS, t + POST_MS, linked_to=len(windows) - 1))
        else:
            cur.end_ms = min(cur.end_ms + EXTEND_MS, cur.start_ms + MAX_MS)
    return windows


@dataclass
class SensorState:
    generation: int = 0
    enabled: bool = False


@dataclass
class CameraInterval:
    generation: int
    on_ms: int
    off_ms: int | None = None


class ProtocolError(Exception):
    """A message that breaks the contract (as opposed to a harmless duplicate)."""


@dataclass
class SessionView:
    session_id: str
    sensors: dict[str, SensorState] = field(
        default_factory=lambda: {s: SensorState() for s in ("camera", "microphone", "aec")}
    )
    camera_intervals: list[CameraInterval] = field(default_factory=list)
    anchors: list[dict[str, Any]] = field(default_factory=list)
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_cursor: int = 1
    buffered: dict[int, dict[str, Any]] = field(default_factory=dict)
    seen_controls: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_audio_chunk: dict[int, int] = field(default_factory=dict)  # microphone generation -> chunk
    audio_chunks: int = 0
    audio_gaps: int = 0  # chunks skipped: audio the PC dropped
    ignored: Counter = field(default_factory=Counter)
    rejected: Counter = field(default_factory=Counter)

    def camera_generation_at(self, capture_ms: int) -> int | None:
        """Camera consent at a timestamp on the PC capture clock."""
        for interval in reversed(self.camera_intervals):
            if interval.on_ms <= capture_ms:
                if interval.off_ms is None or capture_ms < interval.off_ms:
                    return interval.generation
                break
        return None

    # ---- entry point -------------------------------------------------------

    def receive(self, direction: str, msg: dict[str, Any]) -> None:
        """Apply one message. Never raises for bad input; it counts it instead."""
        try:
            kind = msg.get("kind")
            expected = "pc->server" if kind in PC_TO_SERVER else "server->pc"
            if direction != expected:
                raise ProtocolError("wrong_direction")
            if msg.get("session_id") != self.session_id:
                raise ProtocolError("wrong_session")
            handler = getattr(self, f"_on_{kind}", None)
            if handler is None:
                raise ProtocolError("unknown_kind")
            handler(msg)
        except ProtocolError as exc:
            self.rejected[str(exc)] += 1

    # ---- PC -> server ------------------------------------------------------

    def _on_playback_anchor(self, msg: dict[str, Any]) -> None:
        self.anchors.append(msg["playback"] | {"capture_ms": msg["capture_ms"]})

    def _on_control_update(self, msg: dict[str, Any]) -> None:
        key = msg["idempotency_key"]
        if key in self.seen_controls:
            if self.seen_controls[key] != msg:
                raise ProtocolError("idempotency_key_reused")
            self.ignored["duplicate_control"] += 1
            return
        sensor = self.sensors[msg["sensor"]]
        if msg["generation"] <= sensor.generation:
            raise ProtocolError("stale_generation")
        if msg["enabled"] == sensor.enabled:
            raise ProtocolError("no_state_change")
        self.seen_controls[key] = msg
        sensor.generation, sensor.enabled = msg["generation"], msg["enabled"]
        if msg["sensor"] == "camera":
            if msg["enabled"]:
                self.camera_intervals.append(CameraInterval(msg["generation"], msg["capture_ms"]))
            elif self.camera_intervals and self.camera_intervals[-1].off_ms is None:
                self.camera_intervals[-1].off_ms = msg["capture_ms"]

    def _on_audio_chunk(self, msg: dict[str, Any]) -> None:
        # R8: audio is only uploaded while the microphone is on, in its current generation.
        mic = self.sensors["microphone"]
        if not mic.enabled or msg["generation"] != mic.generation:
            raise ProtocolError("stale_audio_generation")
        last = self.last_audio_chunk.get(msg["generation"], -1)
        if msg["chunk"] <= last:
            self.ignored["duplicate_audio"] += 1
            return
        self.audio_gaps += msg["chunk"] - last - 1
        self.last_audio_chunk[msg["generation"]] = msg["chunk"]
        self.audio_chunks += 1

    # ---- server -> PC ------------------------------------------------------

    def _on_control_ack(self, msg: dict[str, Any]) -> None:
        if msg["idempotency_key"] not in self.seen_controls:
            raise ProtocolError("ack_for_unknown_control")

    def _on_event_update(self, msg: dict[str, Any]) -> None:
        cursor = msg["cursor"]
        if cursor < self.next_cursor:
            self.ignored["duplicate_cursor"] += 1
            return
        if cursor > self.next_cursor:
            if cursor in self.buffered:
                self.ignored["duplicate_cursor"] += 1
            else:
                self.buffered[cursor] = msg
            return
        self._apply_event(msg["event"])
        self.next_cursor += 1
        while self.next_cursor in self.buffered:
            self._apply_event(self.buffered.pop(self.next_cursor)["event"])
            self.next_cursor += 1

    def _apply_event(self, ev: dict[str, Any]) -> None:
        # A rejected change still consumes its cursor: the cursor tracks the
        # server's committed log, not what this client accepted.
        try:
            self._check_event_shape(ev)
            old = self.events.get(ev["event_id"])
            if old is None:
                mic = self.sensors["microphone"]
                if not mic.enabled or ev["generations"]["microphone"] != mic.generation:
                    raise ProtocolError("stale_microphone_generation")
                self.events[ev["event_id"]] = ev
                return
            if ev["revision"] == old["revision"]:
                self.ignored["duplicate_revision"] += 1
                return
            if ev["revision"] < old["revision"]:
                self.ignored["stale_revision"] += 1
                return
            if old["phase"] == "FINAL":
                raise ProtocolError("update_after_final")
            for path in IMMUTABLE_EVENT_FIELDS:
                if _dig(ev, path) != _dig(old, path):
                    raise ProtocolError("immutable_field_changed")
            old_t, new_t = old["capture"]["triggers_ms"], ev["capture"]["triggers_ms"]
            if new_t[: len(old_t)] != old_t:
                raise ProtocolError("triggers_rewritten")
            self.events[ev["event_id"]] = ev
        except ProtocolError as exc:
            self.rejected[str(exc)] += 1

    @staticmethod
    def _check_event_shape(ev: dict[str, Any]) -> None:
        cap = ev["capture"]
        t = cap["triggers_ms"]
        if any(b <= a for a, b in zip(t, t[1:])):
            raise ProtocolError("triggers_not_ascending")
        if cap["start_ms"] > t[0]:
            raise ProtocolError("start_after_trigger")
        if cap["end_ms"] is not None and cap["end_ms"] < cap["start_ms"]:
            raise ProtocolError("end_before_start")

    # ---- derived views -----------------------------------------------------

    def clips(self) -> list[dict[str, Any]]:
        """Clip plan per event, in event arrival order.

        expected_status is what the clip should become once EM-2 cuts and
        verifies the file: pending (event not FINAL yet), ready, partial, or
        unavailable. The real status only moves to ready/partial after the
        saved file is finalized and checked.
        """
        out = []
        for ev in self.events.values():
            cam_gen = ev["generations"]["camera"]
            interval = next((c for c in self.camera_intervals if c.generation == cam_gen), None)
            for i, w in enumerate(clip_windows(ev["capture"]["triggers_ms"])):
                status, reason, missing = self._coverage(w, cam_gen, interval)
                if status in ("ready", "partial") and ev["phase"] != "FINAL":
                    status = "pending"
                out.append(
                    {
                        "event_id": ev["event_id"],
                        "index": i,
                        "window_ms": [w.start_ms, w.end_ms],
                        "linked_to": w.linked_to,
                        "expected_status": status,
                        "reason": reason,
                        "missing_ms": missing,
                    }
                )
        return out

    @staticmethod
    def _coverage(w: ClipWindow, cam_gen: int | None, interval: CameraInterval | None):
        if cam_gen is None:
            return "unavailable", "camera_off", [[w.start_ms, w.end_ms]]
        if interval is None:
            return "unavailable", "unknown_camera_generation", [[w.start_ms, w.end_ms]]
        if interval.off_ms is not None and interval.off_ms < w.end_ms:
            # Disabling the camera cancels unfinished clip jobs and clears unsaved video (R5).
            return "unavailable", "camera_stopped", [[w.start_ms, w.end_ms]]
        if interval.on_ms >= w.end_ms:
            return "unavailable", "camera_off", [[w.start_ms, w.end_ms]]
        if interval.on_ms > w.start_ms:
            return "partial", "before_camera_activation", [[w.start_ms, interval.on_ms]]
        return "ready", None, []

    def summary(self) -> dict[str, Any]:
        return {
            "events": {
                eid: {"phase": ev["phase"], "revision": ev["revision"], "type": ev["reaction"]["type"]}
                for eid, ev in self.events.items()
            },
            "clips": self.clips(),
            "ignored": dict(self.ignored),
            "rejected": dict(self.rejected),
            "unapplied_cursors": sorted(self.buffered),
        }


def _dig(obj: dict[str, Any], path: tuple[str, ...]) -> Any:
    for key in path:
        obj = obj[key]
    return obj


def replay(scenario: dict[str, Any]) -> SessionView:
    view = SessionView(scenario["session_id"])
    for step in scenario["messages"]:
        view.receive(step["dir"], step["msg"])
    return view
