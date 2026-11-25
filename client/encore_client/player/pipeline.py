"""GStreamer playback pipeline for the controlled player.

One top-level Gst.Pipeline decodes a local file:

    uridecodebin3 ─┬─ video: queue ! videoconvert ! videoscale ! RGBx ! appsink
                   └─ audio: queue ! audioconvert ! audioresample ! S16LE 48 kHz
                             ! volume ! webrtcechoprobe (if installed) ! audio sink
                             ! volume ! tee ─┬─ audio sink                (otherwise)
                                             └─ S16LE mono ! appsink echo_ref

The audio branch runs at 48 kHz through webrtcechoprobe so the echo
canceller (R1) can process microphone blocks inside this same
pipeline, which the probe requires (Submission B, Requirement 1).
Ubuntu 24.04 and Homebrew ship no probe, so then the branch copies the
audio to an unsynced appsink and the linear canceller (linear_echo.py, EM-10)
cancels against it by running time. Either tap sits after the volume control
so the reference matches what the speakers actually play.

There is no Qt and no GLib main loop here: the UI calls poll() from a timer,
which handles bus messages, emits playback anchors and returns the newest
video frame. That keeps all state changes on the caller's thread.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from ..clock import monotonic_ms  # noqa: E402
from .anchors import AnchorTracker, content_identity  # noqa: E402
from .echo import EchoFilter  # noqa: E402
from .linear_echo import LinearEchoPath, ReferenceBuffer  # noqa: E402

Gst.init(None)

NS_PER_MS = 1_000_000
AUDIO_CAPS = "audio/x-raw,format=S16LE,rate=48000,layout=interleaved"
VIDEO_CAPS = "video/x-raw,format=RGBx,pixel-aspect-ratio=1/1"
# volume=0 makes the volume element emit GAP buffers, which stops the audio
# sink (and with it the pipeline clock) from starting. -80 dB is inaudible.
MIN_VOLUME = 1e-4


@dataclass(frozen=True)
class Frame:
    width: int
    height: int
    stride: int
    data: bytes
    pts_ms: int | None


def echo_probe_available() -> bool:
    return Gst.ElementFactory.find("webrtcechoprobe") is not None


class PlayerPipeline:
    """Plays one local file and reports where playback is (playback anchors)."""

    def __init__(
        self,
        session_id: str,
        on_anchor: Callable[[dict[str, Any]], None] | None = None,
        audio_sink: str = "autoaudiosink",
        heartbeat_ms: int = 10_000,
        clock: Callable[[], int] = monotonic_ms,
    ) -> None:
        self.tracker = AnchorTracker(session_id, on_anchor, clock)
        self.clock = clock
        self.audio_sink_desc = audio_sink
        self.probe_name = f"encore_echo_{id(self):x}"
        self._echo = None
        self._reference_end_ns = None
        self._echo_failed = False
        self.heartbeat_ms = heartbeat_ms
        # "available": WebRTC probe + filter. "linear": the plugins are missing (Ubuntu 24.04,
        # Homebrew), so the player taps its own audio and uses the linear canceller (EM-10).
        self.echo_reference = "available" if echo_probe_available() else "linear"
        self._reference = ReferenceBuffer()
        self.pipeline: Gst.Pipeline | None = None
        self.path: Path | None = None
        self.rate = 1.0
        self.want_playing = False
        self.eos = False
        self.error: str | None = None
        self.has_audio = False
        self.has_video = False
        self._appsink: Gst.Element | None = None
        self._volume: Gst.Element | None = None
        self._report_on_async_done = False
        self._last_frame_pts: int | None = None

    # ---- lifecycle ---------------------------------------------------------

    def open(self, path: str | os.PathLike, title: str | None = None) -> None:
        """Load a file and preroll it paused at 0. Starts a new playback epoch."""
        self.close()
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        content_id, default_title = content_identity(self.path)
        self.tracker.new_content(content_id, title or default_title)
        self.rate, self.want_playing, self.eos, self.error = 1.0, False, False, None
        self.has_audio = self.has_video = False

        self.pipeline = Gst.Pipeline.new("encore-player")
        src = Gst.ElementFactory.make("uridecodebin3", "decoder")
        if src is None:
            raise RuntimeError("GStreamer element uridecodebin3 is missing (gstreamer1.0-plugins-base)")
        src.set_property("uri", Gst.filename_to_uri(str(self.path)))
        src.connect("pad-added", self._on_pad_added)
        self.pipeline.add(src)
        self._report_on_async_done = True
        if self.pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
            self.error = "could not start the pipeline"

    def close(self) -> None:
        self.reset_echo()
        self._reference.reset()
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        self.pipeline = None
        self._appsink = self._volume = None
        self._last_frame_pts = None

    # ---- controls ----------------------------------------------------------

    def play(self) -> None:
        if self.pipeline is None:
            return
        if self.eos:
            self.seek(0)
        self.want_playing = True
        self.pipeline.set_state(Gst.State.PLAYING)

    def pause(self) -> None:
        if self.pipeline is None:
            return
        self.want_playing = False
        self.reset_echo()
        self.pipeline.set_state(Gst.State.PAUSED)

    def toggle(self) -> None:
        self.pause() if self.want_playing else self.play()

    def seek(self, position_ms: int) -> bool:
        """Jump to position_ms. Starts a new epoch; the anchor follows when the seek lands."""
        if self.pipeline is None:
            return False
        duration = self.duration_ms()
        position_ms = max(0, int(position_ms))
        if duration:
            position_ms = min(position_ms, duration)
        self.reset_echo()
        ok = self.pipeline.seek(
            self.rate,
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            Gst.SeekType.SET,
            position_ms * NS_PER_MS,
            Gst.SeekType.NONE,
            0,
        )
        if ok:
            self.eos = False
            self.tracker.seeked()
            self._report_on_async_done = True
        return ok

    def set_rate(self, rate: float) -> bool:
        """Change playback speed. Same epoch if the change is instant, new epoch otherwise."""
        if self.pipeline is None or rate <= 0:
            return False
        if rate == self.rate:
            return True
        self.reset_echo()
        instant = self.pipeline.seek(
            rate,
            Gst.Format.TIME,
            Gst.SeekFlags.INSTANT_RATE_CHANGE,
            Gst.SeekType.NONE,
            0,
            Gst.SeekType.NONE,
            0,
        )
        if instant:
            self.rate = rate
            self._report()
            return True
        position = self.position_ms() or 0
        self.rate = rate
        return self.seek(position)

    def set_volume(self, volume: float) -> None:
        if self._volume is not None:
            self._volume.set_property("volume", max(MIN_VOLUME, min(volume, 2.0)))

    # ---- queries -----------------------------------------------------------

    def position_ms(self) -> int | None:
        if self.pipeline is None:
            return None
        ok, pos = self.pipeline.query_position(Gst.Format.TIME)
        return pos // NS_PER_MS if ok and pos >= 0 else None

    def duration_ms(self) -> int | None:
        if self.pipeline is None:
            return None
        ok, dur = self.pipeline.query_duration(Gst.Format.TIME)
        return dur // NS_PER_MS if ok and dur > 0 else None

    @property
    def title(self) -> str | None:
        return self.tracker.title

    # ---- main-thread pump --------------------------------------------------

    def poll(self) -> Frame | None:
        """Handle pending bus messages, send due anchors, return the newest frame."""
        if self.pipeline is None:
            return None
        bus = self.pipeline.get_bus()
        while (msg := bus.pop()) is not None:
            self._on_message(msg)
        last = self.tracker.last
        if (
            last is not None
            and last.state == "playing"
            and self.clock() - last.capture_ms >= self.heartbeat_ms
        ):
            self._report()
        return self._pull_frame()

    # ---- internals ---------------------------------------------------------

    def _report(self, playing: bool | None = None) -> None:
        pos = self.position_ms()
        if pos is None:
            last = self.tracker.last
            pos = last.position_ms if last is not None else 0
        self.tracker.report(pos, self.rate, self.want_playing if playing is None else playing)

    def _on_message(self, msg: Gst.Message) -> None:
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            self.error = f"{err.message}" + (f" ({debug})" if debug else "")
            self.want_playing = False
            if self.pipeline is not None:
                self.pipeline.set_state(Gst.State.PAUSED)
        elif t == Gst.MessageType.CLOCK_LOST:
            self.reset_echo()
        elif t == Gst.MessageType.EOS:
            self.eos = True
            self.want_playing = False
            if self.pipeline is not None:
                self.pipeline.set_state(Gst.State.PAUSED)
        elif t == Gst.MessageType.ASYNC_DONE and msg.src == self.pipeline:
            # uridecodebin3 can finish its own state change before any stream
            # branch exists; the real preroll comes with a later ASYNC_DONE.
            if self._report_on_async_done and (self.has_video or self.has_audio):
                self._report_on_async_done = False
                self._report()
        elif t == Gst.MessageType.STATE_CHANGED and msg.src == self.pipeline:
            old, new, pending = msg.parse_state_changed()
            # Ignore the transient PLAYING -> PAUSED a flushing seek causes, and
            # anything while a seek/preroll report is still outstanding.
            if pending != Gst.State.VOID_PENDING or self._report_on_async_done:
                return
            last = self.tracker.last
            if new == Gst.State.PLAYING and (last is None or last.state != "playing"):
                self._report(playing=True)
            elif old == Gst.State.PLAYING and new == Gst.State.PAUSED:
                if last is None or last.state != "paused":
                    self._report(playing=False)

    def _on_pad_added(self, _src: Gst.Element, pad: Gst.Pad) -> None:
        # Runs on a GStreamer streaming thread.
        kind = _pad_kind(pad)
        if kind == "video" and not self.has_video:
            desc = (
                f"queue ! videoconvert ! videoscale ! {VIDEO_CAPS} ! "
                "appsink name=video_out sync=true max-buffers=1 drop=true emit-signals=false enable-last-sample=true"
            )
        elif kind == "audio" and not self.has_audio:
            if self.echo_reference == "available":
                desc = (
                    f"queue ! audioconvert ! audioresample ! {AUDIO_CAPS} ! "
                    f"volume name=volume ! webrtcechoprobe name={self.probe_name} ! {self.audio_sink_desc}"
                )
            else:
                # Copy what will be played, after the volume, into an appsink. sync=false: the
                # copy arrives as soon as it is decoded, up to the sink queue's length (about
                # 1 s) ahead, and ReferenceBuffer files it by running time. A synced appsink
                # behind a leaky queue would never catch up: the sink branch's queue lets
                # upstream run 1 s ahead, so the small leaky queue dropped every buffer before
                # its time came and almost nothing reached the reference.
                desc = (
                    f"queue ! audioconvert ! audioresample ! {AUDIO_CAPS} ! volume name=volume ! "
                    f"tee name=echo_tee ! queue ! {self.audio_sink_desc} "
                    "echo_tee. ! queue leaky=downstream max-size-time=2000000000 max-size-buffers=0 "
                    "max-size-bytes=0 ! audioconvert ! audio/x-raw,format=S16LE,rate=48000,channels=1 ! "
                    "appsink name=echo_ref sync=false emit-signals=true max-buffers=0 drop=false"
                )
        else:
            desc = "fakesink async=false sync=false"
        branch = Gst.parse_bin_from_description(desc, True)
        if kind == "audio" and (probe_element := branch.get_by_name(self.probe_name)) is not None:
            probe_element.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._reference_buffer)
        if kind == "audio" and (tap := branch.get_by_name("echo_ref")) is not None:
            tap.connect("new-sample", self._reference_sample)
        self.pipeline.add(branch)
        branch.sync_state_with_parent()
        if pad.link(branch.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            self.error = f"could not link {kind} stream"
            return
        if kind == "video" and not self.has_video:
            self.has_video, self._appsink = True, branch.get_by_name("video_out")
        elif kind == "audio" and not self.has_audio:
            self.has_audio, self._volume = True, branch.get_by_name("volume")

    def _reference_buffer(self, pad, info):
        buffer = info.get_buffer()
        event = pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
        if event is not None and buffer is not None and buffer.pts != Gst.CLOCK_TIME_NONE:
            segment = event.parse_segment()
            duration = buffer.duration if buffer.duration != Gst.CLOCK_TIME_NONE else 0
            end_ns = segment.to_running_time(Gst.Format.TIME, buffer.pts + duration)
            if end_ns != Gst.CLOCK_TIME_NONE:
                self._reference_end_ns = end_ns
        return Gst.PadProbeReturn.OK

    def _reference_sample(self, sink):
        """Linear mode: keep the samples to be played by running time (GStreamer streaming thread)."""
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buffer, segment = sample.get_buffer(), sample.get_segment()
        if buffer is None or buffer.pts == Gst.CLOCK_TIME_NONE or abs(segment.rate - 1.0) > 1e-6:
            # At other speeds running time and sample count disagree; no reference then.
            self._reference.reset()
            return Gst.FlowReturn.OK
        start_ns = segment.to_running_time(Gst.Format.TIME, buffer.pts)
        if start_ns == Gst.CLOCK_TIME_NONE:
            return Gst.FlowReturn.OK
        data = buffer.extract_dup(0, buffer.get_size())
        self._reference.push(data, start_ns)
        self._reference_end_ns = start_ns + len(data) // 2 * Gst.SECOND // 48_000
        return Gst.FlowReturn.OK

    def reset_echo(self):
        if self._echo is not None:
            self._echo.close()
            self._echo = None
        self._reference_end_ns = None
        self._echo_failed = False

    def cancel_echo(self, pcm, capture_ms):
        """Filter complete mono 48 kHz blocks on the PC capture clock."""
        pipeline = self.pipeline
        clock = pipeline.get_clock() if pipeline is not None else None
        if not self.want_playing or self.eos or clock is None or self._reference_end_ns is None:
            if self._echo is not None:
                self.reset_echo()
            return pcm, "reference_unavailable"
        now_ns = clock.get_time() - pipeline.get_base_time()
        pts = now_ns - (self.clock() - capture_ms) * Gst.MSECOND
        if self.echo_reference == "linear":
            # The tap runs ahead of the speakers, so its end says nothing about the
            # microphone's lag; the buffer itself knows whether the block is covered.
            if pts < 0:
                self.reset_echo()
                return pcm, "reference_unavailable"
            if self._echo is None:
                self._echo = LinearEchoPath(self._reference)
            return self._echo.process(pcm, pts)
        if pts < 0 or not -250 * Gst.MSECOND <= self._reference_end_ns - pts <= 500 * Gst.MSECOND:
            self.reset_echo()
            return pcm, "reference_unavailable"
        if self._echo_failed:
            return pcm, "reference_unavailable"
        try:
            if self._echo is None:
                self._echo = EchoFilter(pipeline, self.probe_name)
            return self._echo.process(pcm, pts)
        except RuntimeError:
            self.reset_echo()
            self._echo_failed = True
            return pcm, "reference_unavailable"

    def _pull_frame(self) -> Frame | None:
        sink = self._appsink
        if sink is None:
            return None
        # Drain the queue: appsink holds back EOS until it is empty.
        sample = None
        while (pulled := sink.emit("try-pull-sample", 0)) is not None:
            sample = pulled
        if sample is None:
            # Nothing new while playing, or paused: last-sample is the frame
            # most recently rendered or prerolled (e.g. right after a seek).
            sample = sink.get_property("last-sample")
        if sample is None or sample.get_buffer() is None:
            return None
        pts = sample.get_buffer().pts
        if pts == self._last_frame_pts:
            return None
        self._last_frame_pts = pts
        return _to_frame(sample)


def _pad_kind(pad: Gst.Pad) -> str:
    stream = pad.get_stream()
    if stream is not None:
        stype = stream.get_stream_type()
        if stype & Gst.StreamType.VIDEO:
            return "video"
        if stype & Gst.StreamType.AUDIO:
            return "audio"
    caps = pad.get_current_caps() or pad.query_caps(None)
    if caps is not None and caps.get_size() > 0:
        name = caps.get_structure(0).get_name()
        if name.startswith("video/"):
            return "video"
        if name.startswith("audio/"):
            return "audio"
    return "other"


def _to_frame(sample: Gst.Sample) -> Frame | None:
    caps = sample.get_caps()
    buf = sample.get_buffer()
    if caps is None or buf is None:
        return None
    s = caps.get_structure(0)
    ok_w, width = s.get_int("width")
    ok_h, height = s.get_int("height")
    if not (ok_w and ok_h):
        return None
    ok, info = buf.map(Gst.MapFlags.READ)
    if not ok:
        return None
    try:
        data = bytes(info.data)
    finally:
        buf.unmap(info)
    pts = buf.pts // NS_PER_MS if buf.pts != Gst.CLOCK_TIME_NONE else None
    return Frame(width, height, width * 4, data, pts)
