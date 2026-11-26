"""PC camera consent, pinned rolling disk buffer and checked event-to-clip publication."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from ..clock import monotonic_ms
from ..session.contract import clip_windows
from ..ui.formatting import format_ms
from .capture import GstCamera, Segment
from .storage import CaptureDirectory, atomic_index, index_lock, recover

HANDLE_MS = 5_000
RETENTION_MS = 35_000
BUDGET_BYTES = 128 * 1024 * 1024
MAX_PENDING_JOBS = 128
FACE_HISTORY = 20  # RGB frames kept for audio pairing: 2 s at the camera's 10 fps cap


def ranges(intervals):
    result = []
    for a, b in sorted(intervals):
        if b <= a:
            continue
        if result and result[-1][1] >= a:
            result[-1][1] = max(result[-1][1], b)
        else:
            result.append([a, b])
    return result


def missing(start, end, coverage):
    gaps = []
    cursor = start
    for a, b in coverage:
        if b <= cursor or a >= end:
            continue
        if cursor < a:
            gaps.append([cursor, a])
        cursor = max(cursor, b)
    if cursor < end:
        gaps.append([cursor, end])
    return gaps


def select_video(video, low, high):
    """Map actual capture coverage onto the media timeline, excluding holes."""
    selected = []
    offset = 0
    previous_end = low
    for fragment in sorted(video, key=lambda item: item.start):
        start = max(fragment.start, previous_end)
        end = min(fragment.end, high)
        if end > start:
            selected.append({
                "path": str(fragment.path), "start": fragment.start, "cut_start": start,
                "cut_end": end, "offset": offset,
            })
            previous_end = end
            offset += end - start
    return selected, ranges((part["cut_start"], part["cut_end"]) for part in selected)


@dataclass
class Job:
    row: dict
    final: bool


class DesktopRecorder:
    def __init__(self, index_path, session, *, may_commit=None, camera_factory=GstCamera,
                 clock=monotonic_ms, budget_bytes=BUDGET_BYTES):
        self.index = Path(index_path).resolve()
        self.session = session
        self.may_commit = may_commit or (lambda _clip: True)
        self.camera_factory = camera_factory
        self.clock = clock
        self.budget = budget_bytes
        self.listener = self.camera = self.directory = self.generation = self.face = None
        self.faces = deque(maxlen=FACE_HISTORY)  # recent (capture ms, RGB), oldest first
        self.activated_ms = None
        self.microphone_on = None
        self.error = None
        self.jobs = {}
        self.revisions = {}
        self.video = []
        self.audio = []
        self.exporting = None
        try:
            recover(self.index)
        except (OSError, ValueError, KeyError) as exc:
            self.error = str(exc)

    def set_listener(self, listener):
        self.listener = listener

    def permission_granted(self):
        device = Path(os.environ.get("ENCORE_CAMERA_DEVICE", "/dev/video0"))
        # A missing/busy device is an open failure, not an OS permission denial.
        return not device.exists() or os.access(device, os.R_OK | os.W_OK)

    def _admitted(self):
        return self.generation is not None and self.listener.admission_open(self.generation)

    def open(self, generation):
        if not self.listener.admission_open(generation):
            return
        self.generation = generation
        self.error = None
        try:
            self.index.parent.mkdir(parents=True, exist_ok=True)
            self.directory = CaptureDirectory(self.index)
            self.camera = self.camera_factory(Path(self.directory.name))
            self.microphone_on = self.session.view.sensors["microphone"].enabled
        except Exception as exc:
            self.listener.on_open_failed(generation, f"Cannot open camera: {exc}")

    def close(self, generation):
        if self.generation != generation:
            return
        self.generation = self.face = self.activated_ms = None
        self.faces.clear()
        if self.camera is not None:
            self.camera.close()
            self.camera = None
        if self.exporting is not None:
            process = self.exporting[0]
            if process.poll() is None:
                process.kill()
            process.wait()
            self.exporting = None
        self.video.clear()
        self.audio.clear()
        for clip_id, job in list(self.jobs.items()):
            self._unavailable(clip_id, job.row, "camera_stopped")
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None
        self.listener.on_stopped(generation)

    def latest_face(self, start_ms=None, end_ms=None):
        """The newest camera frame as (generation, capture ms, RGB), or None.

        With a span, the newest frame captured in [start_ms, end_ms): the span
        of an audio chunk. A chunk is sent only after the read that completes
        it, so by then the newest frame can be later than the whole chunk."""
        if not self._admitted():
            return None
        if start_ms is None:
            if self.face and 0 <= self.clock() - self.face[0] < 1200:
                return self.generation, *self.face
            return None
        for image_ms, rgb in reversed(self.faces):
            if image_ms < start_ms:
                break
            if image_ms < end_ms:
                return self.generation, image_ms, rgb
        return None

    def feed_audio(self, pcm, capture_ms):
        if not self._admitted() or not self.session.view.sensors["microphone"].enabled or not pcm:
            return
        # A microphone chunk can straddle an explicit camera enable or a clock resync.
        if self.activated_ms is None:
            return
        first = max(capture_ms, self.activated_ms, self.audio[-1].end if self.audio else capture_ms)
        end = capture_ms + len(pcm) // 32
        if end <= first:
            return
        try:
            path = Path(self.directory.name) / f"mic-{first}.pcm"
            data = pcm[(first - capture_ms) * 32:(end - capture_ms) * 32]
            path.write_bytes(data)
            self.audio.append(Segment(path, first, end, len(data)))
            self._evict()
        except OSError as exc:
            self._lost(exc)

    def poll(self, events):
        try:
            if self._admitted():
                microphone = self.session.view.sensors["microphone"].enabled
                if microphone != self.microphone_on:
                    self.camera.new_fragment()
                    self.microphone_on = microphone
                segments, face = self.camera.poll()
                self.video.extend(segments)
                if face is not None:
                    if self.face is None:
                        self.activated_ms = face[0]
                        self.listener.on_first_frame(self.generation, face[0])
                    if not self.faces or face[0] > self.faces[-1][0]:
                        self.faces.append(face)
                    self.face = face
                    if self.clock() - face[0] > 3_000:
                        raise OSError("no new camera frames for 3 seconds")
            for event in events:
                self._observe(event)
            if self._admitted():
                self._finish_export()
                if self.exporting is None:
                    self._schedule()
                self._evict()
        except (KeyError, ValueError, OSError, RuntimeError) as exc:
            self._lost(exc)

    def _lost(self, exc):
        self.error = str(exc)
        if self.generation is not None:
            if self.face is None:
                self.listener.on_open_failed(self.generation, self.error)
            else:
                self.listener.on_device_lost(self.generation, self.error)

    def _observe(self, event):
        session_id = self.session.session_id
        event_id = event["event_id"]
        key = (session_id, event_id)
        if event["revision"] <= self.revisions.get(key, 0):
            return
        playback = event.get("playback") or {}
        generation = event["generations"]["camera"]
        windows = clip_windows(event["capture"]["triggers_ms"])
        for i, window in enumerate(windows):
            clip_id = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([session_id, event_id, i])).hex
            row = dict(clip_id=clip_id, event_type=event["reaction"]["type"],
                       intensity=event["intensity"]["combined"], session_id=session_id,
                       title=playback.get("title") or "", timecode=format_ms(playback.get("timecode_start_ms")),
                       event_ids=[event_id], part=i, linked_to=window.linked_to,
                       window_ms=[window.start_ms, window.end_ms], status="pending", path=None)
            if not self.may_commit(clip_id):
                continue
            if generation != self.generation or not self._admitted():
                self._store(row | {"status": "unavailable", "reason": "camera_off"})
            else:
                row["capture_owner"] = Path(self.directory.name).name
                if clip_id not in self.jobs and len(self.jobs) >= MAX_PENDING_JOBS:
                    self._store(row | {"status": "unavailable", "reason": "too_many_pending_clips"})
                    continue
                if self._store(row):
                    sealed = i < len(windows) - 1 or event["phase"] == "FINAL"
                    self.jobs[clip_id] = Job(row, sealed)

        self.revisions[key] = event["revision"]

    def _store(self, row, *, media=None):
        """Merge one row under a lock. Preserve fixtures, other writers and finalized rows."""
        with index_lock(self.index):
            data = json.loads(self.index.read_text()) if self.index.exists() else {"clips": []}
            rows = data["clips"]
            old = next((r for r in rows if r["clip_id"] == row["clip_id"]), None)
            if old and (old["status"] != "pending" or old.get("capture_owner") != row.get("capture_owner")):
                return False
            if not self.may_commit(row["clip_id"]):
                return False
            if old is None:
                rows.append(row)
            else:
                rows[rows.index(old)] = row
            dest = self.index.parent / row["path"] if media else None
            atomic_index(self.index, data, media=media, destination=dest)
            return True

    def _unavailable(self, clip_id, row, reason):
        self.jobs.pop(clip_id, None)
        try:
            self._store(row | {"status": "unavailable", "reason": reason})
        except (KeyError, ValueError, OSError) as exc:
            self.error = str(exc)

    def _schedule(self):
        now = self.clock()
        for clip_id, job in list(self.jobs.items()):
            row = job.row
            start, end = row["window_ms"]
            if not self.may_commit(clip_id):
                del self.jobs[clip_id]
                continue
            if not job.final or now < end + HANDLE_MS:
                continue
            if not self.video or self.video[-1].end < end + HANDLE_MS:
                if now < end + HANDLE_MS + 3_000:  # wait for splitmux to close the last GOP
                    continue
            segments, coverage = select_video(self.video, start - HANDLE_MS, end + HANDLE_MS)
            gaps = missing(start, end, coverage)
            if gaps == [[start, end]]:
                self._unavailable(clip_id, row, "no_footage")
                continue
            sounds = [(str(sound.path), sound.start, sound.end) for sound in self.audio
                      if any(lo < sound.end and hi > sound.start for lo, hi in coverage)]
            root = Path(self.directory.name)
            manifest = root / "export.json"
            manifest.write_text(json.dumps({"video": segments, "audio": sounds}))
            output = root / "export.webm"
            with (root / "export.log").open("w") as err:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "encore_client.clips.export", str(manifest), str(output)],
                    cwd=Path(__file__).resolve().parents[2], stderr=err, stdout=subprocess.DEVNULL)

            def position(t):
                return sum(max(0, min(t, b) - a) for a, b in coverage)

            result = row | {
                "status": "partial" if gaps else "ready",
                "path": f"{clip_id}.webm",
                "coverage_ms": coverage,
                "gaps": [[(a - start) / 1000, (b - start) / 1000] for a, b in gaps],
                "audio_coverage_ms": ranges((max(a, lo), min(b, hi)) for _, a, b in sounds
                                            for lo, hi in coverage),
                "trim_ms": [position(start), position(end)],
            }
            self.exporting = proc, output, result, self.clock()
            return

    def _finish_export(self):
        if self.exporting is None:
            return
        proc, output, row, started = self.exporting
        if proc.poll() is None:
            if self.clock() - started < 120_000 and self.may_commit(row["clip_id"]):
                return
            proc.kill()
            proc.wait()
        clip_id = row["clip_id"]
        try:
            if proc.returncode or not output.is_file() or not output.stat().st_size:
                raise OSError("clip encoding or decode check failed")
            if self._admitted() and self.may_commit(clip_id):
                actual = json.loads(output.with_suffix(".coverage.json").read_text())
                start, end = row["window_ms"]
                gaps = missing(start, end, actual)
                if gaps == [[start, end]]:
                    raise OSError("no decoded frames inside the requested clip")
                row.update(status="partial" if gaps else "ready", coverage_ms=actual,
                           gaps=[[(a - start) / 1000, (b - start) / 1000] for a, b in gaps])
                self._store(row, media=output)
        except (KeyError, ValueError, OSError) as exc:
            self.error = str(exc)
            self._unavailable(clip_id, row | {"path": None}, self.error)
        finally:
            self.jobs.pop(clip_id, None)
            self.exporting = None
            output.unlink(missing_ok=True)

    def _evict(self):
        windows = [j.row["window_ms"] for j in self.jobs.values()]
        total = sum(s.size for s in self.video + self.audio)
        live = sorted(self.video + self.audio, key=lambda s: s.end)
        for s in live:
            if any(a - HANDLE_MS < s.end and s.start < b + HANDLE_MS for a, b in windows):
                continue
            if s.end >= self.clock() - RETENTION_MS and total <= self.budget:
                break
            s.path.unlink(missing_ok=True)
            (self.audio if s.path.suffix == ".pcm" else self.video).remove(s)
            total -= s.size
        if total > self.budget:
            raise OSError("Camera disk budget exhausted by pinned footage")

    def shutdown(self):
        if self.generation is not None:
            self.close(self.generation)
