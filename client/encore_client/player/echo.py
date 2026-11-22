"""Feed timestamped 48 kHz mic PCM to the controlled player's WebRTC echo canceller.

The DSP is a filter in the probe's pipeline. The Qt capture thread's caller
pushes complete 10 ms blocks synchronously, without adding a second clock,
capture device, or sink that could keep the player from reaching EOS.
"""
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst


BLOCK_BYTES = 480 * 2
WARMUP_MS = 500


class EchoFilter:
    def __init__(self, pipeline, probe_name):
        self.pipeline = pipeline
        self.dsp = Gst.ElementFactory.make("webrtcdsp")
        if self.dsp is None:
            raise RuntimeError("webrtcdsp unavailable")
        self.dsp.set_property("probe", probe_name)
        self.dsp.set_property("noise-suppression", False)
        self.dsp.set_property("gain-control", False)
        self.dsp.set_property("high-pass-filter", False)
        self.source = Gst.Pad.new("audience-input", Gst.PadDirection.SRC)
        self.sink = Gst.Pad.new("cleaned-audio", Gst.PadDirection.SINK)
        self.sink.set_chain_function_full(self._capture)
        self.sink.set_event_function_full(lambda pad, parent, event: True)
        self.output = bytearray()
        self.processed_ms = 0
        self.pipeline.add(self.dsp)
        try:
            self.source.set_active(True)
            self.sink.set_active(True)
            self.source.link(self.dsp.get_static_pad("sink"))
            self.dsp.get_static_pad("src").link(self.sink)
            caps = Gst.Caps.from_string("audio/x-raw,format=S16LE,rate=48000,channels=1,layout=interleaved")
            segment = Gst.Segment()
            segment.init(Gst.Format.TIME)
            if not (self.dsp.sync_state_with_parent()
                    and self.source.push_event(Gst.Event.new_stream_start("encore-audience-mic"))
                    and self.source.push_event(Gst.Event.new_caps(caps))
                    and self.source.push_event(Gst.Event.new_segment(segment))):
                raise RuntimeError("echo filter could not negotiate microphone PCM")
        except Exception:
            self.close()
            raise

    def _capture(self, pad, parent, buffer):
        self.output.extend(buffer.extract_dup(0, buffer.get_size()))
        return Gst.FlowReturn.OK

    def process(self, pcm, pts_ns):
        if len(pcm) % BLOCK_BYTES:
            raise ValueError("echo filter requires complete 10 ms blocks")
        self.output.clear()
        buffer = Gst.Buffer.new_wrapped(pcm)
        buffer.pts = pts_ns
        buffer.duration = len(pcm) // BLOCK_BYTES * 10 * Gst.MSECOND
        if self.source.push(buffer) != Gst.FlowReturn.OK or len(self.output) != len(pcm):
            raise RuntimeError("echo filter dropped microphone samples")
        status = "active" if self.processed_ms >= WARMUP_MS else "warming_up"
        self.processed_ms += len(pcm) // BLOCK_BYTES * 10
        return bytes(self.output), status

    def close(self):
        self.dsp.set_state(Gst.State.NULL)
        self.source.set_active(False)
        self.sink.set_active(False)
        self.source.unlink(self.dsp.get_static_pad("sink"))
        self.dsp.get_static_pad("src").unlink(self.sink)
        self.pipeline.remove(self.dsp)
