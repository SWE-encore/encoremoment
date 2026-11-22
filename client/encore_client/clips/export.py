"""Cancellable export worker. Decode/trim closed VP8 fragments and mux admitted microphone PCM."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from .capture import gst


def _input_samples(path):
    Gst = gst()
    pipeline = Gst.parse_launch(
        "filesrc name=file ! matroskademux ! vp8dec ! video/x-raw,format=I420 ! "
        "appsink name=frames sync=false max-buffers=2")
    pipeline.get_by_name("file").set_property("location", str(path))
    sink = pipeline.get_by_name("frames")
    try:
        pipeline.set_state(Gst.State.PLAYING)
        while True:
            sample = sink.emit("try-pull-sample", 8 * Gst.SECOND)
            if sample is not None:
                yield sample
                continue
            error = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
            if error:
                raise RuntimeError(error.parse_error()[0].message)
            if sink.get_property("eos"):
                return
            raise RuntimeError("camera fragment decode timed out")
    finally:
        pipeline.set_state(Gst.State.NULL)


def _finish(pipeline):
    Gst = gst()
    done = pipeline.get_bus().timed_pop_filtered(
        30 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    if done is None:
        raise RuntimeError("clip finalization timed out")
    if done.type == Gst.MessageType.ERROR:
        raise RuntimeError(done.parse_error()[0].message)


def export(manifest, destination):
    Gst = gst()
    video, audio = manifest["video"], manifest["audio"]
    writer = Gst.parse_launch(
        "webmmux name=mux ! filesink name=out "
        "appsrc name=video format=time block=true max-bytes=1500000 "
        "caps=video/x-raw,format=I420,width=640,height=360,framerate=10/1 ! "
        "queue ! vp8enc deadline=1 keyframe-max-dist=10 target-bitrate=700000 ! queue ! mux."
        + (" appsrc name=audio format=time block=true caps=audio/x-raw,format=S16LE,"
           "rate=16000,channels=1,layout=interleaved ! queue ! audioconvert ! "
           "opusenc frame-size=20 ! queue ! mux." if audio else ""))
    writer.get_by_name("out").set_property("location", str(destination))
    errors = []
    worker = None

    def write_audio():
        src = writer.get_by_name("audio")
        try:
            for path, start, end in audio:
                for fragment in video:
                    a, b = max(start, fragment["cut_start"]), min(end, fragment["cut_end"])
                    if a >= b:
                        continue
                    with open(path, "rb") as stream:
                        stream.seek((a - start) * 32)  # 16 mono 16-bit samples per millisecond
                        data = stream.read((b - a) * 32)
                    if len(data) != (b - a) * 32:
                        raise RuntimeError("incomplete microphone chunk")
                    buffer = Gst.Buffer.new_wrapped(data)
                    buffer.pts = (fragment["offset"] + a - fragment["cut_start"]) * Gst.MSECOND
                    buffer.duration = (b - a) * Gst.MSECOND
                    if src.emit("push-buffer", buffer) != Gst.FlowReturn.OK:
                        raise RuntimeError("audio encoder stopped")
        except Exception as exc:
            errors.append(exc)
        finally:
            src.emit("end-of-stream")

    try:
        writer.set_state(Gst.State.PLAYING)
        if audio:
            worker = threading.Thread(target=write_audio, daemon=True)
            worker.start()
        src = writer.get_by_name("video")
        decoded_ranges = []
        for seg in video:
            baseline = None
            for sample in _input_samples(seg["path"]):
                buf = sample.get_buffer()
                if baseline is None:
                    baseline = buf.pts
                ms = seg["start"] + (buf.pts - baseline) // Gst.MSECOND
                duration = buf.duration // Gst.MSECOND if buf.duration != Gst.CLOCK_TIME_NONE else 100
                a = max(ms, seg["cut_start"])
                b = min(ms + duration, seg["cut_end"])
                if a >= b:
                    continue
                out = buf.copy_deep()
                out.pts = (seg["offset"] + a - seg["cut_start"]) * Gst.MSECOND
                out.dts = Gst.CLOCK_TIME_NONE
                out.duration = (b - a) * Gst.MSECOND
                if src.emit("push-buffer", out) != Gst.FlowReturn.OK:
                    raise RuntimeError("video encoder stopped")
                if decoded_ranges and decoded_ranges[-1][1] >= a:
                    decoded_ranges[-1][1] = max(decoded_ranges[-1][1], b)
                else:
                    decoded_ranges.append([a, b])
        src.emit("end-of-stream")
        _finish(writer)
        if errors:
            raise errors[0]
        if not decoded_ranges:
            raise RuntimeError("no decodable camera frames")
    finally:
        writer.set_state(Gst.State.NULL)
        if worker is not None:
            worker.join(2)

    # Read the finished file through the actual decoders, including its audio track.
    check = Gst.parse_launch(
        "uridecodebin name=file file. ! queue ! videoconvert ! fakesink sync=false"
        + (" file. ! queue ! audioconvert ! fakesink sync=false" if audio else ""))
    check.get_by_name("file").set_property("uri", Path(destination).resolve().as_uri())
    try:
        check.set_state(Gst.State.PLAYING)
        _finish(check)
    finally:
        check.set_state(Gst.State.NULL)
    return decoded_ranges


if __name__ == "__main__":
    output = Path(sys.argv[2])
    coverage = export(json.loads(Path(sys.argv[1]).read_text()), output)
    output.with_suffix(".coverage.json").write_text(json.dumps(coverage))
