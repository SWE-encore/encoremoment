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
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Callable

from .vision import MAGIC, unpack_pair
from .audio import HOP_MS, AudioWindows, RoomBaseline, rms_db
from .contract import PC_TO_SERVER, SessionView, decode_audio_frame, schema_errors
from .detector import DetectorConfig, Frame, ReactionDetector
from .fusion import TabRules, visual_for_hop

_FACE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="encore-face")
FACE_GRACE_S = .08

IDLE_S = 2.0  # no audio for this long while an event is open: the stream stopped


class ServerSession:
    def __init__(
        self,
        session_id: str,
        model: Callable[[bytes], dict[str, float]],
        config: DetectorConfig | None = None,
        *,
        face_model=None,
        smile_threshold: float = TabRules.smile,
        quiet_ms: int = TabRules.quiet_ms,
        clock: Callable[[], float] = time.monotonic,
        idle_s: float = IDLE_S,
        on_frame: Callable[[Frame], None] | None = None,
    ) -> None:
        """``on_frame`` sees every analysed frame (scores, intensity) before the
        detector does: a debugging view for tools such as the test bench. It is
        not part of the contract and nothing is sent to the PC for it.
        """
        self.session_id = session_id
        self.face_model = face_model
        self.tab_rules = TabRules(smile=smile_threshold, quiet_ms=quiet_ms) if face_model is not None else None
        self.smiles = deque(maxlen=128)
        self.pending_face = None
        self.face_detected = None
        self.face_infer_ms = None
        self.face_chunk = None
        self.face_frames = 0
        self.faces_seen = 0
        self.model = model
        self.view = SessionView(session_id)
        self.detector = ReactionDetector(config, self.tab_rules)
        self.baseline = RoomBaseline()
        self.windows = AudioWindows()
        self.log: list[dict[str, Any]] = []
        self.invalid = 0
        self.audio_chunks = 0
        self.idle_s = idle_s
        self._clock = clock
        self._last_audio: float | None = None
        self._on_frame = on_frame

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
            rgb = None
            if data.startswith(MAGIC):
                camera_generation, image_ms, rgb, data = unpack_pair(data)
                if self.face_model is None or camera_generation != self._camera_generation():
                    raise ValueError("unavailable or stale camera")
            header, pcm = decode_audio_frame(data)
        except ValueError:
            self.invalid += 1
            return []
        if header.get("session_id") != self.session_id or schema_errors(header):
            self.invalid += 1
            return []
        if self._apply(header)[0] != "applied":
            return []  # old microphone generation (R8) or a retried chunk
        out = []
        if self.tab_rules and self.windows.next_chunk is not None and self.windows.discontinuous(header):
            self._reset_media()
            out += self._commit(self.detector.close_all())
        self._collect_face()
        if rgb is not None and self.pending_face is None:
            chunk_end = header["capture_ms"] + header["samples"] * 1000 // header["sample_rate"]
            if header["capture_ms"] <= image_ms <= chunk_end + HOP_MS:
                future = _FACE_POOL.submit(self._infer_face, rgb)
                self.pending_face = (image_ms, header["chunk"], future)
        self.audio_chunks += 1
        self._last_audio = self._clock()
        for w in self.windows.add(header, pcm):
            started = time.monotonic()
            scores = self.model(w.pcm)
            audio_ms = round((time.monotonic() - started) * 1000, 1)
            frame = Frame(w.start_ms, w.end_ms, scores, self.baseline.strength(rms_db(w.pcm)),
                          w.generation, self._camera_generation(), w.aec)
            if self.tab_rules and frame.camera is not None:
                center = (w.start_ms + w.end_ms) // 2
                grace = 0
                raw = frame.scores.get("laughter", 0)
                if self.pending_face and self.tab_rules.assisted_audio * self.tab_rules.release_ratio <= raw < self.tab_rules.strong_audio:
                    face_ms, _, _ = self.pending_face
                    if face_ms is not None and center - HOP_MS // 2 <= face_ms < center + HOP_MS // 2:
                        grace = FACE_GRACE_S
                self._collect_face(grace)
                frame.visual = visual_for_hop(self.smiles, center)
            if self._on_frame is not None:
                self._on_frame(frame)
            changes = self.detector.push(frame)
            if rgb is not None:
                rules = self.tab_rules
                active = self.detector.open.get("laughter")
                out.append({"kind": "demo_scores", "laughter": frame.scores.get("laughter", 0),
                            "smile": frame.visual, "face_detected": self.face_detected,
                            "face_chunk": self.face_chunk, "face_infer_ms": self.face_infer_ms,
                            "audio_infer_ms": audio_ms, "window_end_ms": w.end_ms,
                            "face_frames": self.face_frames, "faces_seen": self.faces_seen,
                            "fusion_window_ms": rules.window_ms, "evidence_seconds": self.detector.fusion.total,
                            "target_seconds": rules.equivalent_hops * HOP_MS / 1000,
                            "smile_threshold": rules.smile, "laughter_threshold": rules.strong_audio,
                            "assisted_audio_threshold": rules.assisted_audio,
                            "release_ratio": rules.release_ratio, "quiet_ms": rules.quiet_ms,
                            "quiet_elapsed_ms": w.end_ms - active.last_active_ms if active else None})
            out += self._commit(changes)
        return out

    def on_idle(self) -> list[dict[str, Any]]:
        """Close open events once audio has stopped arriving (file ended, connection stalled)."""
        idle_s = self.idle_s
        if self.tab_rules:
            idle_s = max(idle_s, self.tab_rules.quiet_ms / 1000)
        if self._last_audio is not None and self._clock() - self._last_audio >= idle_s:
            self._reset_media()
            self._last_audio = None
            return self._commit(self.detector.close_all())
        return []

    def end(self) -> list[dict[str, Any]]:
        """The PC ended the session: close what is open."""
        self._reset_media()
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
        if status == "applied" and msg["sensor"] in ("microphone", "camera"):
            if self.face_model is not None or (msg["sensor"] == "microphone" and not msg["enabled"]):
                self._reset_media()
                out += self._commit(self.detector.close_all())
        return out

    def _infer_face(self, rgb):
        start = time.monotonic()
        result = self.face_model(rgb)
        return result, round((time.monotonic() - start) * 1000, 1)

    def _collect_face(self, grace=0):
        if self.pending_face is None:
            return
        image_ms, chunk, future = self.pending_face
        if grace and not future.done():
            wait([future], timeout=grace)
        if not future.done():
            return
        self.pending_face = None
        if image_ms is None:  # completed after a capture boundary
            return
        try:
            score, self.face_infer_ms = future.result()
            self.face_chunk = chunk
        except Exception:
            self.face_detected = None
            return
        self.face_frames += 1
        self.face_detected = score is not None
        self.faces_seen += int(self.face_detected)
        self.smiles.append((image_ms, score))

    def _reset_media(self):
        self.windows.reset()
        self.smiles.clear()
        self.face_detected = None
        self.face_infer_ms = self.face_chunk = None
        if self.pending_face is not None:
            _, _, future = self.pending_face
            future.cancel()
            self.pending_face = (None, None, future)
        if self.detector.fusion:
            self.detector.fusion.reset()

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
            if self.tab_rules and ev["reaction"]["type"] == "laughter":
                rules = self.tab_rules
                ev["source"].update(model=self.model.name + ("+mediapipe-face" if ev["generations"]["camera"] is not None else ""),
                                    config=f"fusion-{rules.window_ms}ms-{rules.equivalent_hops}h-a{rules.strong_audio:g}-a{rules.assisted_audio:g}"
                                           f"-s{rules.smile:g}-q{rules.quiet_ms}")
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
