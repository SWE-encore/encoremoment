"""The server's side of one viewing session (EM-3, EM-11, EM-12).

A ``ServerSession`` keeps

- a mirror of the PC's state, built with the shared contract receiver
  (``SessionView``), so sensor generations, playback anchors and audio
  admission follow exactly the rules the PC applies (R5, R8);
- the committed result log: each event change gets the next cursor, and a
  PC that reconnects can be sent everything after the cursor it has (R9, R11);
- the audio analysis: windows, then the model, then the detector (R2, R3).

It does no I/O. ``on_audio`` calls the model and may take tens of
milliseconds, so the WebSocket server runs it off the event loop.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any, Callable

from .audio import AudioWindows, RoomBaseline, rms_db
from .contract import PC_TO_SERVER, SessionView, decode_audio_frame, schema_errors
from .detector import DetectorConfig, Frame, ReactionDetector

IDLE_S = 2.0  # no audio for this long while an event is open: the stream stopped


class ServerSession:
    def __init__(
        self,
        session_id: str,
        model: Callable[[bytes], dict[str, float]],
        config: DetectorConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        idle_s: float = IDLE_S,
    ) -> None:
        self.session_id = session_id
        self.model = model
        self.view = SessionView(session_id)
        self.detector = ReactionDetector(config)
        self.baseline = RoomBaseline()
        self.windows = AudioWindows()
        self.log: list[dict[str, Any]] = []
        self.invalid = 0
        self.audio_chunks = 0
        self.idle_s = idle_s
        self._clock = clock
        self._last_audio: float | None = None

    def results_after(self, cursor: int) -> list[dict[str, Any]]:
        """Committed event changes after ``cursor``, for a (re)connecting PC."""
        return self.log[max(cursor, 0) :]

    # ---- PC -> server ------------------------------------------------------------

    def on_text(self, text: str) -> list[dict[str, Any]]:
        """A text message from the PC. Returns the messages to send back."""
        try:
            msg = json.loads(text)
        except ValueError:
            msg = None
        if (not isinstance(msg, dict) or msg.get("kind") not in PC_TO_SERVER
                or msg.get("kind") == "audio_chunk" or msg.get("session_id") != self.session_id
                or schema_errors(msg)):
            self.invalid += 1
            return []
        if msg["kind"] == "control_update":
            return self._on_control(msg)
        self._apply(msg)  # playback_anchor
        return []

    def on_audio(self, data: bytes) -> list[dict[str, Any]]:
        """A binary audio frame from the PC. Runs the model on every complete window."""
        try:
            header, pcm = decode_audio_frame(data)
        except ValueError:
            self.invalid += 1
            return []
        if header.get("session_id") != self.session_id or schema_errors(header):
            self.invalid += 1
            return []
        if self._apply(header)[0] != "applied":
            return []  # old microphone generation (R8) or a retried chunk
        self.audio_chunks += 1
        self._last_audio = self._clock()
        out = []
        for w in self.windows.add(header, pcm):
            frame = Frame(w.start_ms, w.end_ms, self.model(w.pcm), self.baseline.strength(rms_db(w.pcm)),
                          w.generation, self._camera_generation(), w.aec)
            out += self._commit(self.detector.push(frame))
        return out

    def on_idle(self) -> list[dict[str, Any]]:
        """Close open events once audio has stopped arriving (file ended, connection stalled)."""
        if self.detector.open and self._last_audio is not None and self._clock() - self._last_audio >= self.idle_s:
            self.windows.reset()
            return self._commit(self.detector.close_all())
        return []

    def end(self) -> list[dict[str, Any]]:
        """The PC ended the session: close what is open."""
        self.windows.reset()
        return self._commit(self.detector.close_all())

    # ---- internals ---------------------------------------------------------------

    def _on_control(self, msg: dict[str, Any]) -> list[dict[str, Any]]:
        status, reason = self._apply(msg)
        ack = {
            "schema": "encore.v1",
            "kind": "control_ack",
            "session_id": self.session_id,
            "idempotency_key": msg["idempotency_key"],
            "sensor": msg["sensor"],
            "generation": msg["generation"],
            "status": status,
        }
        if reason:
            ack["reason"] = reason
        out = [ack]
        if status == "applied" and msg["sensor"] == "microphone" and not msg["enabled"]:
            # R8: no new audio events from here on; open ones close with what was heard.
            self.windows.reset()
            out += self._commit(self.detector.close_all())
        return out

    def _apply(self, msg: dict[str, Any]) -> tuple[str, str | None]:
        """Apply a PC message to the mirror. Returns (applied|duplicate|rejected, reason)."""
        rejected, ignored = Counter(self.view.rejected), Counter(self.view.ignored)
        self.view.receive("pc->server", msg)
        new_rejections = self.view.rejected - rejected
        if new_rejections:
            return "rejected", next(iter(new_rejections))
        if self.view.ignored - ignored:
            return "duplicate", None
        return "applied", None

    def _camera_generation(self) -> int | None:
        camera = self.view.sensors["camera"]
        return camera.generation if camera.enabled else None

    def _commit(self, changes) -> list[dict[str, Any]]:
        out = []
        for ev, aec in changes:
            ev = ev | {
                "playback": self._playback(ev),
                "source": {"model": self.model.name, "config": self.detector.config.name, "aec": aec},
            }
            msg = {
                "schema": "encore.v1",
                "kind": "event_update",
                "session_id": self.session_id,
                "cursor": len(self.log) + 1,
                "event": ev,
            }
            self.log.append(msg)
            out.append(msg)
        return out

    def _playback(self, ev: dict[str, Any]) -> dict[str, Any]:
        """Content timecode from the PC's anchors (R3). Never guessed: pending without one."""
        start, end = ev["capture"]["start_ms"], ev["capture"]["end_ms"]
        a = self._anchor_at(start)
        if a is None:
            return {"status": "pending"}
        b = self._anchor_at(end) if end is not None else None
        same = b is not None and (b["epoch"], b["content_id"]) == (a["epoch"], a["content_id"])
        return {
            "status": "mapped",
            "epoch": a["epoch"],
            "content_id": a["content_id"],
            "title": a["title"],
            "timecode_start_ms": _timecode(a, start),
            "timecode_end_ms": _timecode(b, end) if same else None,
        }

    def _anchor_at(self, t: int) -> dict[str, Any] | None:
        best = None
        for a in self.view.anchors:
            if a["capture_ms"] <= t and (best is None or a["capture_ms"] >= best["capture_ms"]):
                best = a
        return best


def _timecode(anchor: dict[str, Any], t: int) -> int:
    if anchor["state"] != "playing":
        return anchor["position_ms"]
    return max(0, anchor["position_ms"] + round(anchor["rate"] * (t - anchor["capture_ms"])))
