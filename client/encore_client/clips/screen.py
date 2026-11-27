"""Screen recording for reaction clips (the Screen view).

While the user has it on, ``ScreenCapture`` records the whole screen (X11) into
one-second fragments in a capture folder next to the clip index, and
``ScreenRecorder`` keeps only the last ``RETENTION_MS`` of them plus what clips
still need. For each finished camera clip of this run (``ready``/``partial``),
it joins the fragments that cover the same capture time span, without
re-encoding (``python -m encore_client.clips.screen OUTPUT FRAGMENT...``), into
``<clip_id>.screen.webm`` and adds it to the clip's row:

    "screen": {"path": "<clip_id>.screen.webm", "coverage_ms": [[start, end], ...]}

The file name is also added to ``temp_paths``, so deleting the clip deletes it
(R7). Turning the screen off, ending the session or closing the app removes the
capture folder. No system sound is recorded; the camera clip has the room sound.

``coverage_ms`` lists the capture times the screen file holds, back to back. The
camera recorder and exporter (recorder.py, capture.py, export.py) are not
changed. screen_map.py turns a clip's camera and screen coverage into "screen
file position at each moment of the camera clip".
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ..clock import monotonic_ms
from ..platform_support import screen_capture_unavailable_reason
from .capture import Segment, gst
from .recorder import HANDLE_MS, RETENTION_MS, ranges
from .storage import CaptureDirectory, atomic_index, index_lock

BUDGET_BYTES = 256 * 1024 * 1024
FINISHED = ("ready", "partial")
CLOSE_WAIT_MS = 3_000      # the last fragment of a span closes up to a second after it
EXPORT_TIMEOUT_MS = 120_000
WIDTH = 1280               # recorded width; the height keeps the screen's shape
SOURCE = "ximagesrc use-damage=false show-pointer=true"


def unavailable_reason(environ=None, platform=None) -> str | None:
    """Why the screen cannot be recorded here, or None."""
    reason = screen_capture_unavailable_reason(platform, environ)
    if reason is not None:
        return reason
    try:
        if gst().ElementFactory.find("ximagesrc") is None:
            return "the GStreamer ximagesrc plugin is not installed"
    except (ImportError, ValueError) as exc:
        return f"GStreamer is not available: {exc}"
    return None


# ---- recording --------------------------------------------------------------------

class ScreenCapture:
    """The screen in one-second VP8 fragments (closed GOPs), like the camera."""

    def __init__(self, directory: Path, *, source: str = SOURCE):
        Gst = self.Gst = gst()
        self.pipeline = Gst.parse_launch(
            f"{source} ! videoconvert ! videoscale ! videorate ! "
            f"video/x-raw,format=I420,width={WIDTH},pixel-aspect-ratio=1/1,framerate=10/1 ! "
            "vp8enc deadline=1 keyframe-max-dist=10 target-bitrate=1500000 ! "
            "splitmuxsink name=ring max-size-time=1000000000 send-keyframe-requests=true "
            "async-finalize=true muxer-factory=webmmux")
        self.ring = self.pipeline.get_by_name("ring")
        self.ring.set_property("location", str(directory / "screen-%08d.webm"))
        self.opened = {}
        self.clock_origin = None
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.close()
            raise RuntimeError("GStreamer could not start screen recording")

    def poll(self) -> list[Segment]:
        Gst = self.Gst
        if self.clock_origin is None and self.pipeline.get_clock() and self.pipeline.get_base_time():
            elapsed_ns = self.pipeline.get_clock().get_time() - self.pipeline.get_base_time()
            self.clock_origin = monotonic_ms() - elapsed_ns // Gst.MSECOND
        segments = []
        bus = self.pipeline.get_bus()
        while (msg := bus.pop()) is not None:
            if msg.type == Gst.MessageType.ERROR:
                raise RuntimeError(msg.parse_error()[0].message)
            if msg.type != Gst.MessageType.ELEMENT:
                continue
            info = msg.get_structure()
            name = info.get_name()
            if name not in ("splitmuxsink-fragment-opened", "splitmuxsink-fragment-closed"):
                continue
            path = Path(info.get_value("location"))
            ms = info.get_value("running-time") // Gst.MSECOND
            if name.endswith("opened"):
                self.opened[path] = ms
            else:
                start = self.opened.pop(path, ms)
                end = (start + info.get_value("fragment-duration") // Gst.MSECOND
                       if info.has_field("fragment-duration") else ms)
                if end > start and self.clock_origin is not None:
                    segments.append(Segment(path, self.clock_origin + start, self.clock_origin + end,
                                            path.stat().st_size))
        return segments

    def close(self):
        self.pipeline.set_state(self.Gst.State.NULL)


class ScreenRecorder:
    """The Screen switch, the rolling screen buffer and one screen file per clip.

    The workspace adds each session's ID to ``sessions``, calls ``poll`` on its
    timer and ``turn_off`` at End; ``may_commit`` is the deletion manager's check
    (a clip being deleted gets no screen file)."""

    def __init__(self, index_path, *, may_commit=None, capture_factory=ScreenCapture,
                 clock=monotonic_ms, budget_bytes=BUDGET_BYTES):
        self.index = Path(index_path).resolve()
        self.may_commit = may_commit or (lambda _clip_id: True)
        self.capture_factory = capture_factory
        self.clock = clock
        self.budget = budget_bytes
        self.sessions: set[str] = set()
        self.capture = self.directory = None
        self.video: list[Segment] = []
        self.error: str | None = None
        self.exporting = None      # (process, output, clip_id, coverage, started)
        self.done: set[str] = set()   # clips handled (a screen file, or nothing to cut)
        self._rows: list[dict] = []
        self._stamp = None

    @property
    def on(self) -> bool:
        return self.capture is not None

    def turn_on(self) -> bool:
        if self.capture is not None:
            return True
        self.error = None
        try:
            self.index.parent.mkdir(parents=True, exist_ok=True)
            self.directory = CaptureDirectory(self.index)
            self.capture = self.capture_factory(Path(self.directory.name))
        except Exception as exc:  # noqa: BLE001 (a device/plugin failure is reported, not raised)
            self.error = f"Cannot record the screen: {exc}"
            self._close()
            return False
        return True

    def turn_off(self) -> None:
        """Stop recording and delete the buffer; screen files already cut stay."""
        self._close()

    def _close(self):
        if self.exporting is not None:
            process = self.exporting[0]
            if process.poll() is None:
                process.kill()
            process.wait()
            self.exporting = None
        if self.capture is not None:
            self.capture.close()
            self.capture = None
        self.video.clear()
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None

    def poll(self) -> None:
        if self.capture is None:
            return
        try:
            self.video.extend(self.capture.poll())
            self._read_index()
            self._finish_export()
            if self.exporting is None:
                self._start_export()
            self._evict()
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            self.error = f"Screen recording stopped: {exc}"
            self._close()

    # ---- which clips need a screen file ---------------------------------------------

    def _read_index(self):
        try:
            st = self.index.stat()
        except FileNotFoundError:
            self._rows = []
            return
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp != self._stamp:
            self._stamp = stamp
            self._rows = [row for row in json.loads(self.index.read_text()).get("clips", [])
                          if isinstance(row, dict) and row.get("session_id") in self.sessions]

    def _wanted(self, row) -> bool:
        return (row.get("status") in FINISHED and "screen" not in row
                and isinstance(row.get("coverage_ms"), list) and row.get("coverage_ms")
                and row.get("clip_id") not in self.done)

    def _span(self, row):
        coverage = row["coverage_ms"]
        return int(coverage[0][0]), int(coverage[-1][1])

    # ---- cutting one clip's screen file ---------------------------------------------

    def _start_export(self):
        newest = max((s.end for s in self.video), default=None)
        for row in filter(self._wanted, self._rows):
            clip_id = row["clip_id"]
            start, end = self._span(row)
            if not self.may_commit(clip_id):
                self.done.add(clip_id)
                continue
            if newest is None or (newest < end and self.clock() < end + CLOSE_WAIT_MS):
                continue   # the screen fragments up to the clip's end are not closed yet
            fragments = sorted((s for s in self.video if s.end > start and s.start < end),
                               key=lambda s: s.start)
            if not fragments:
                self.done.add(clip_id)   # the screen was not recorded during this clip
                continue
            coverage = ranges([s.start, s.end] for s in fragments)
            root = Path(self.directory.name)
            output = root / "screen-export.webm"
            with (root / "screen-export.log").open("w") as err:
                process = subprocess.Popen(
                    [sys.executable, "-m", "encore_client.clips.screen", str(output),
                     *(str(s.path) for s in fragments)],
                    cwd=Path(__file__).resolve().parents[2], stderr=err, stdout=subprocess.DEVNULL)
            self.exporting = process, output, clip_id, coverage, self.clock()
            return

    def _finish_export(self):
        if self.exporting is None:
            return
        process, output, clip_id, coverage, started = self.exporting
        if process.poll() is None:
            if self.clock() - started < EXPORT_TIMEOUT_MS:
                return
            process.kill()
            process.wait()
        self.exporting = None
        self.done.add(clip_id)
        try:
            if process.returncode or not output.is_file() or not output.stat().st_size:
                raise OSError("the screen fragments could not be joined")
            self._store(clip_id, output, coverage)
        except (OSError, ValueError) as exc:
            self.error = f"Screen clip for {clip_id[:8]}: {exc}"
        finally:
            output.unlink(missing_ok=True)

    def _store(self, clip_id, output, coverage):
        name = f"{clip_id}.screen.webm"
        with index_lock(self.index):
            data = json.loads(self.index.read_text())
            row = next((r for r in data.get("clips", []) if r.get("clip_id") == clip_id), None)
            if row is None or row.get("status") not in FINISHED or not self.may_commit(clip_id):
                return
            row["screen"] = {"path": name, "coverage_ms": coverage}
            row["temp_paths"] = [*row.get("temp_paths", []), name]
            atomic_index(self.index, data, media=output, destination=self.index.parent / name)

    # ---- the rolling buffer ---------------------------------------------------------

    def _evict(self):
        pinned = [(a - HANDLE_MS, b + HANDLE_MS) for row in self._rows
                  if row.get("status") == "pending" and isinstance(row.get("window_ms"), list)
                  for a, b in [row["window_ms"]]]
        pinned += [self._span(row) for row in filter(self._wanted, self._rows)]
        total = sum(s.size for s in self.video)
        for s in sorted(self.video, key=lambda s: s.end):
            if any(lo < s.end and s.start < hi for lo, hi in pinned):
                continue
            if s.end >= self.clock() - RETENTION_MS and total <= self.budget:
                break
            s.path.unlink(missing_ok=True)
            self.video.remove(s)
            total -= s.size
        if total > self.budget:
            raise OSError("screen recording disk budget exhausted")


def join_fragments(output: Path, fragments: list[Path]) -> None:
    """One WebM file of ``fragments`` back to back, without re-encoding."""
    Gst = gst()
    parts = " ".join(f"filesrc name=part{i} ! matroskademux ! queue ! join." for i in range(len(fragments)))
    pipeline = Gst.parse_launch(f"concat name=join ! queue ! webmmux ! filesink name=out {parts}")
    pipeline.get_by_name("out").set_property("location", str(output))
    for i, fragment in enumerate(fragments):
        pipeline.get_by_name(f"part{i}").set_property("location", str(fragment))
    try:
        pipeline.set_state(Gst.State.PLAYING)
        done = pipeline.get_bus().timed_pop_filtered(
            60 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        if done is None:
            raise RuntimeError("joining the screen fragments timed out")
        if done.type == Gst.MessageType.ERROR:
            raise RuntimeError(done.parse_error()[0].message)
    finally:
        pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    join_fragments(Path(sys.argv[1]), [Path(arg) for arg in sys.argv[2:]])
