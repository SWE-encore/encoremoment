"""Explicitly opened GStreamer camera with one-second, independently decodable disk fragments."""
import os
from dataclasses import dataclass
from pathlib import Path

from ..clock import monotonic_ms


def gst():
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    return Gst


@dataclass(frozen=True)
class Segment:
    path: Path
    start: int
    end: int
    size: int


class GstCamera:
    def __init__(self, directory: Path, *, source="v4l2src"):
        Gst = self.Gst = gst()
        # Both branches share this camera and clock. Only the small RGB branch
        # drops stale samples. Dropping encoded buffers would damage a fragment.
        self.pipeline = Gst.parse_launch(
            f"{source} name=camera ! decodebin ! videoconvert ! videoscale ! "
            "videorate drop-only=true max-rate=10 ! "
            "video/x-raw,format=I420,width=640,height=360,framerate=(fraction)[1/1,10/1] ! tee name=t "
            "t. ! queue ! vp8enc deadline=1 keyframe-max-dist=10 target-bitrate=600000 ! "
            "splitmuxsink name=ring max-size-time=1000000000 send-keyframe-requests=true "
            "async-finalize=true muxer-factory=webmmux "
            "t. ! queue leaky=downstream max-size-buffers=1 ! videoscale ! videoconvert ! "
            "video/x-raw,format=RGB,width=320,height=180 ! "
            "appsink name=face sync=false max-buffers=1 drop=true enable-last-sample=false")
        camera = self.pipeline.get_by_name("camera")
        if source == "v4l2src":
            camera.set_property("device", os.environ.get("ENCORE_CAMERA_DEVICE", "/dev/video0"))
        else:  # test source, using the same caps, clock and file sinks
            camera.set_property("is-live", True)
        self.ring = self.pipeline.get_by_name("ring")
        self.ring.set_property("location", str(directory / "%08d.webm"))
        self.source_caps = None
        camera.get_static_pad("src").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._caps_changed)
        self.faces = self.pipeline.get_by_name("face")
        self.opened = {}
        self.clock_origin = None
        self.last_frame = None
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.close()
            raise RuntimeError("GStreamer could not start the camera")

    def _caps_changed(self, _pad, info):
        event = info.get_event()
        if event.type == self.Gst.EventType.CAPS:
            caps = event.parse_caps()
            if self.source_caps is not None and not self.source_caps.is_equal(caps):
                self.new_fragment()
            self.source_caps = caps
        return self.Gst.PadProbeReturn.OK

    def new_fragment(self):
        # Finish the current closed GOP if the microphone or device format changes.
        self.ring.emit("split-after")

    def poll(self):
        Gst = self.Gst
        if self.clock_origin is None and self.pipeline.get_clock() and self.pipeline.get_base_time():
            elapsed_ns = self.pipeline.get_clock().get_time() - self.pipeline.get_base_time()
            self.clock_origin = monotonic_ms() - elapsed_ns // Gst.MSECOND
        segments = []
        bus = self.pipeline.get_bus()
        while (msg := bus.pop()) is not None:
            if msg.type == Gst.MessageType.ERROR:
                raise RuntimeError(msg.parse_error()[0].message)
            if msg.type == Gst.MessageType.EOS:
                raise RuntimeError("camera stream ended")
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
                start = self.opened.pop(path)
                end = (start + info.get_value("fragment-duration") // Gst.MSECOND
                       if info.has_field("fragment-duration") else ms)
                if end > start and self.clock_origin is not None:
                    segments.append(Segment(path, self.clock_origin + start, self.clock_origin + end,
                                            path.stat().st_size))
        sample = self.faces.emit("try-pull-sample", 0)
        if sample is not None and self.clock_origin is not None:
            buf = sample.get_buffer()
            rt = sample.get_segment().to_running_time(Gst.Format.TIME, buf.pts)
            if rt != Gst.CLOCK_TIME_NONE:
                self.last_frame = (self.clock_origin + rt // Gst.MSECOND,
                                   buf.extract_dup(0, buf.get_size()))
        return segments, self.last_frame

    def close(self):
        self.pipeline.set_state(self.Gst.State.NULL)
        self.last_frame = None
