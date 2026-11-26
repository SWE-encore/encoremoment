"""R5/R10 recording lifecycle, including native GStreamer when its plugins are available."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from encore_client.clips.capture import GstCamera, Segment
from encore_client.clips.model import ClipStatus, load_index
from encore_client.clips.recorder import DesktopRecorder
from encore_client.clips.storage import recover
from encore_client.sensing.camera import CameraControl


class Session:
    session_id = "recorder-test"

    def __init__(self):
        self.view = SimpleNamespace(sensors={"microphone": SimpleNamespace(enabled=True)})

    def set_sensor(self, *args, **kwargs):
        pass


class Source:
    def __init__(self, directory, clock):
        self.path = directory
        self.clock = clock
        self.segments = []
        self.splits = 0

    def new_fragment(self):
        self.splits += 1

    def poll(self):
        segments, self.segments = self.segments, []
        return segments, (self.clock(), bytes(320 * 180 * 3))

    def close(self):
        pass


def rig(tmp, budget=1024 * 1024):
    now = [100_000]
    session = Session()
    device = DesktopRecorder(tmp / "index.json", session,
                             camera_factory=lambda directory: Source(directory, lambda: now[0]),
                             clock=lambda: now[0], budget_bytes=budget)
    control = CameraControl(device, session, lambda: True)
    return now, control, device


def event(*, phase="OPEN", revision=1, triggers=(100_000,), generation=1):
    return {"phase": phase, "revision": revision, "generations": {"camera": generation},
            "event_id": "evt", "reaction": {"type": "gasp"}, "intensity": {"combined": 0.5},
            "capture": {"triggers_ms": list(triggers)}, "playback": {"timecode_start_ms": 1_000}}


def segment(source, start, end, data):
    p = source.path / f"fragment-{start}.webm"
    p.write_bytes(data)
    source.segments.append(Segment(p, start, end, len(data)))
    return p


def test_privacy_cancel_and_camera_off_metadata(tmp_path):
    _, ctl, dev = rig(tmp_path)
    dev.feed_audio(b"private", 99_000)
    assert not list(tmp_path.iterdir())
    assert ctl.enable()
    dev.poll([event()])
    assert dev.latest_face() and load_index(dev.index)[0].status is ClipStatus.PENDING
    dev.feed_audio(b"\x01\x00" * 1600, 99_950)
    assert [(s.start, s.end) for s in dev.audio] == [(100_000, 100_050)]
    ctl.disable()
    assert not list(tmp_path.glob(".encore-camera-*"))
    assert not dev.latest_face()
    dev.poll([event(phase="FINAL", revision=2)])
    assert load_index(dev.index)[0].status is ClipStatus.UNAVAILABLE
    assert ctl.enable()
    dev.poll([event(phase="FINAL", revision=2)])
    assert not dev.jobs and not dev.audio
    dev.shutdown()


def test_mic_off_splits_capture_and_prevents_more_audio(tmp_path):
    _, ctl, dev = rig(tmp_path)
    ctl.enable()
    dev.poll([])
    dev.feed_audio(bytes(3_200), 100_000)
    dev.session.view.sensors["microphone"].enabled = False
    dev.feed_audio(bytes(3_200), 100_100)
    dev.poll([])
    assert len(dev.audio) == 1 and dev.camera.splits == 1
    assert dev.latest_face() is not None
    dev.shutdown()


def test_frames_are_offered_by_audio_span_and_forgotten_when_the_camera_stops(tmp_path):
    now, ctl, dev = rig(tmp_path)
    ctl.enable()
    for _ in range(5):  # frames at 100 000 .. 100 400
        dev.poll([])
        now[0] += 100
    assert dev.latest_face(100_050, 100_250)[:2] == (1, 100_200)  # the newest inside the span
    assert dev.latest_face(100_450, 100_950) is None
    ctl.disable()
    assert dev.latest_face(100_000, 100_500) is None
    ctl.enable()
    dev.poll([])  # first frame of the new capture, at 100 500
    assert dev.latest_face(100_000, 100_500) is None  # generation 1 frames are gone
    assert dev.latest_face(100_500, 101_000)[:2] == (dev.generation, 100_500) and dev.generation > 1
    dev.shutdown()


def test_refires_capped_linked_parts_and_duplicate_revisions(tmp_path):
    _, ctl, dev = rig(tmp_path)
    ctl.enable()
    dev.poll([event()])
    triggers = tuple(100_000 + i * 1_000 for i in range(9))
    update = event(revision=2, triggers=triggers)
    dev.poll([update, update, event()])
    rows = json.loads(dev.index.read_text())["clips"]
    assert [(r["window_ms"], r["linked_to"]) for r in rows] == [
        ([85_000, 145_000], None), ([93_000, 118_000], 0)]
    dev.shutdown()


def test_pins_survive_eviction_and_budget_failure_cancels_safely(tmp_path):
    now, ctl, dev = rig(tmp_path, budget=9)
    ctl.enable()
    older = segment(dev.camera, 10_000, 11_000, b"old")
    pinned = segment(dev.camera, 96_000, 97_000, b"keep")
    dev.poll([event()])
    assert not older.exists() and pinned.exists()
    now[0] += 50_000
    dev.poll([])
    assert pinned.exists()
    segment(dev.camera, 97_000, 98_000, b"cannot fit")
    dev.poll([])
    assert "budget" in dev.error and not pinned.exists()
    assert load_index(dev.index)[0].status is ClipStatus.UNAVAILABLE


def test_interrupted_capture_cleanup_and_active_owner(tmp_path):
    _, ctl, dev = rig(tmp_path)
    ctl.enable()
    dev.poll([event()])
    ring = Path(dev.directory.name)
    recover(dev.index)
    assert ring.exists() and load_index(dev.index)[0].status is ClipStatus.PENDING
    clip_id = load_index(dev.index)[0].clip_id
    orphan = tmp_path / f"{clip_id}.webm"
    orphan.write_bytes(b"incomplete commit")
    dev.directory.lock.close()  # OS releases this lock if the recording process dies
    recover(dev.index)
    assert not orphan.exists() and not ring.exists()
    assert load_index(dev.index)[0].status is ClipStatus.UNAVAILABLE


@pytest.mark.parametrize("camera_mode", ["raw", "mjpeg_5fps"])
def test_native_camera_audio_clip_decodes_and_persists(tmp_path, monkeypatch, camera_mode):
    pytest.importorskip("gi")
    from encore_client.clips.capture import gst
    Gst = gst()
    if not all(Gst.ElementFactory.find(name) for name in
               ("videotestsrc", "splitmuxsink", "vp8enc", "vp8dec", "webmmux", "opusenc", "opusdec")):
        pytest.skip("GStreamer media plugins unavailable")
    if camera_mode == "mjpeg_5fps":
        if not all(Gst.ElementFactory.find(name) for name in ("jpegenc", "jpegdec")):
            pytest.skip("GStreamer JPEG plugins unavailable")
        native_launch = Gst.parse_launch

        def compressed_camera(description):
            # Some webcams expose compressed frames and only five frames per second.
            description = description.replace("videotestsrc name=camera",
                "videotestsrc name=camera ! video/x-raw,framerate=5/1 ! jpegenc")
            return native_launch(description)

        monkeypatch.setattr(Gst, "parse_launch", compressed_camera)
    session = Session()
    index = tmp_path / "native.json"
    dev = DesktopRecorder(index, session, camera_factory=lambda d: GstCamera(d, source="videotestsrc"))
    ctl = CameraControl(dev, session, lambda: True)
    ctl.enable()
    try:
        stop = time.monotonic() + 10
        while len(dev.video) < 2 and time.monotonic() < stop and not dev.error:
            dev.poll([])
            time.sleep(0.04)
        assert len(dev.video) >= 2, dev.error
        start = dev.video[0].start
        dev.feed_audio(b"\x00\x02" * 8_000, start + 500)
        # Delayed FINAL: only the tail of the requested window exists on disk.
        ev = event(phase="FINAL", triggers=(start - 8_500,))
        stop = time.monotonic() + 12
        while time.monotonic() < stop:
            dev.poll([ev])
            rows = load_index(index)
            if rows and rows[0].status != ClipStatus.PENDING:
                break
            time.sleep(0.06)
        assert rows[0].status == ClipStatus.PARTIAL, dev.error
        row = json.loads(index.read_text())["clips"][0]
        assert row["window_ms"] == [start - 23_500, start + 1_500]
        assert row["trim_ms"] == [0, 1_500] and row["gaps"] == [[0.0, 23.5]]
        assert row["audio_coverage_ms"] == [[start + 500, start + 1_000]]
        output = index.parent / rows[0].path
        assert output.stat().st_size > 500
        # Both tracks share the capture clock. Here the microphone deliberately
        # starts 500 ms after video. Check the timestamps in the decoded WebM.
        decode = Gst.parse_launch(
            "uridecodebin name=file file. ! queue ! audioconvert ! "
            "appsink name=audio sync=false file. ! queue ! videoconvert ! appsink name=video sync=false")
        decode.get_by_name("file").set_property("uri", output.resolve().as_uri())
        try:
            decode.set_state(Gst.State.PLAYING)
            starts = []
            for name in ("video", "audio"):
                sample = decode.get_by_name(name).emit("try-pull-sample", 3 * Gst.SECOND)
                assert sample is not None
                timestamp = sample.get_segment().to_stream_time(Gst.Format.TIME, sample.get_buffer().pts)
                starts.append(timestamp // Gst.MSECOND)
            assert abs(starts[1] - starts[0] - 500) <= 30  # Opus framing / preskip
        finally:
            decode.set_state(Gst.State.NULL)
        ctl.disable()
        assert output.is_file()
        dev.poll([ev])
        assert len(load_index(index)) == 1
    finally:
        dev.shutdown()
