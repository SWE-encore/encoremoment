"""PC microphone packets carry only permitted camera frames, and work on audio-only deployments."""
import json

from encore_client.session.contract import decode_audio_frame
from encore_client.session.session import Conn, Session
from encore_server.session import ServerSession
from encore_server.vision import RGB_BYTES, unpack_pair


def test_event_capture_pairs_current_camera_and_falls_back_to_audio():
    s = Session("ws://unused", "ses-paired")
    server = ServerSession(s.session_id, lambda pcm: {})
    packets = []

    class Link:
        def send(self, data):
            if isinstance(data, str):
                server.on_text(data)
            else:
                packets.append(data)
                server.on_audio(data)
            return True

    s.state = Conn.CONNECTED
    s._transport = Link()
    s.set_sensor("microphone", True)
    image = bytes(RGB_BYTES)
    created = []

    def frame(start_ms, end_ms):
        created.append((start_ms, end_ms))
        return 1, 1120, image

    s.camera_frame = frame
    pcm = bytes(16_000)
    assert s.send_audio(pcm, 500)
    assert not created
    s.set_sensor("camera", True, capture_ms=1000)
    assert s.send_audio(pcm, 1000)
    gen, when, rgb, audio = unpack_pair(packets[-1])
    assert (gen, when, rgb, decode_audio_frame(audio)[1]) == (1, 1120, image, pcm)
    assert s.send_audio(pcm, 1500)
    assert not packets[-1].startswith(b"EMV1")  # same old frame is never sent for a later chunk
    s.set_sensor("camera", False)
    assert s.send_audio(pcm, 2000)
    assert created == [(1000, 1500), (1500, 2000)]  # each chunk asks for a frame of its own span
    assert all(not p.startswith(b"EMV1") for p in (packets[0], packets[-1]))
    assert server.audio_chunks == 4 and server.invalid == 0
    s._on_message(json.dumps({"kind": "demo_scores", "laughter": .1}))
    assert not s.view.rejected


def test_camera_image_belongs_to_one_audio_interval_and_one_connection():
    now = [1000]
    s = Session("ws://unused", "ses-sync", clock=lambda: now[0])
    packets = []

    class Link:
        def send(self, data):
            if isinstance(data, bytes):
                packets.append(data)
            return True

    s._transport = Link()
    s._on_open()
    s.set_sensor("microphone", True)
    s.set_sensor("camera", True)
    image_ms = 1500
    rgb = bytes(RGB_BYTES)
    s.camera_frame = lambda _start, _end: (1, image_ms, rgb)  # even if the source ignores the span
    pcm = bytes(16_000)

    def send(start):
        assert s.send_audio(pcm, start)
        return unpack_pair(packets[-1])[1] if packets[-1].startswith(b"EMV1") else None

    assert send(1000) is None          # end boundary is in the next chunk
    assert send(1500) == 1500
    assert send(1500) is None          # overlapping capture timestamps
    image_ms = 2350
    assert send(1800) is None          # this image is still in the future
    s._set_state(Conn.DISCONNECTED)
    now[0] = 2400
    s._on_open()                       # latest frame was captured while offline
    assert send(2300) is None
    image_ms = 2400
    assert send(2300) == 2400


def test_workspace_routes_accepted_event_and_timeline_playback(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import pytest
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from encore_client.session.contract import SAMPLES_DIR
    from encore_client.session.session import load_scenario
    from encore_client.ui.workspace import Workspace

    app = QApplication.instance() or QApplication([])
    sample = load_scenario(SAMPLES_DIR / "01_single_event.json")
    session = Session("ws://unused", sample["session_id"])
    index = tmp_path / "index.json"

    class Recorder:
        def __init__(self, _index, _session, *, may_commit):
            self.error = None
            self.events = []
            self.may_commit = may_commit

        def set_listener(self, listener):
            self.listener = listener

        def latest_face(self, start_ms=None, end_ms=None):
            return None

        def poll(self, events):
            self.events = list(events)

        def shutdown(self):
            pass

    workspace = Workspace(session, None, index, recorder_factory=Recorder)
    try:
        for step in sample["messages"]:
            session.view.receive(step["dir"], step["msg"])
        session.state = Conn.CONNECTED
        event_id = next(iter(session.view.events))
        path = tmp_path / "clip.webm"
        path.write_bytes(b"fixture")
        index.write_text(json.dumps({"clips": [{"clip_id": "test", "session_id": session.session_id,
            "event_ids": [event_id], "intensity": 0.8, "event_type": "gasp", "status": "ready",
            "timecode": "00:00:13", "path": path.name}]}))
        workspace.session_panel.refresh()
        workspace.refresh()
        workspace.session_panel.table.selectRow(0)
        assert workspace.recorder.events == [session.view.events[event_id]]
        assert workspace.play_event.isEnabled()
        played = []
        monkeypatch.setattr(workspace.clips, "play_file", played.append)
        workspace.play_event.click()
        assert played == [path] and workspace.tabs.currentWidget() is workspace.clips
        workspace.deletions.delete("test")
        workspace.refresh()
        assert not workspace.play_event.isEnabled()
    finally:
        workspace.shutdown()
        workspace.close()
        app.processEvents()


def test_offline_camera_audio_stays_local_and_cannot_leak_into_reconnect():
    from encore_client.sensing.live_mic import LiveMicrophone

    s = Session("ws://test", "ses-local-audio")
    s.state = Conn.CONNECTED
    s.set_sensor("microphone", True)
    s.set_sensor("camera", True)
    uploaded, recorded = [], []
    s.send_audio = lambda pcm, ms, **kw: (uploaded.append((pcm, ms)) or True)
    mic = LiveMicrophone(s, keep_s=0, on_pcm=lambda pcm, ms: recorded.append((pcm, ms)))

    mic.feed(b"A" * 8000, end_ms=1250)  # half a chunk until disconnection
    s.state = Conn.DISCONNECTED
    mic.feed(b"B" * 16000, end_ms=1750)
    mic.feed(b"C" * 8000, end_ms=2000)
    assert uploaded == []
    s.state = Conn.CONNECTED
    mic.feed(b"D" * 16000, end_ms=2500)
    assert uploaded == [(b"D" * 16000, 2000)]
    assert recorded == [(b"A" * 8000, 1000), (b"B" * 16000, 1250),
                        (b"C" * 8000, 1750), (b"D" * 16000, 2000)]
    s.state = Conn.RECONNECTING
    s.set_sensor("camera", False)
    mic.feed(b"E" * 16000, end_ms=3000)
    s.set_sensor("camera", True)
    s.set_sensor("microphone", False)
    mic.feed(b"F" * 16000, end_ms=3500)
    assert len(uploaded) == 1 and len(recorded) == 4 and not mic.recording


def test_reconnect_between_reads_keeps_only_post_connect_samples_for_upload():
    from encore_client.sensing.live_mic import LiveMicrophone

    now = [1000]
    s = Session("ws://test", "ses-reconnect-capture", clock=lambda: now[0])
    s._set_state(Conn.CONNECTED)
    s.set_sensor("microphone", True)
    s.set_sensor("camera", True)
    uploaded, recorded = [], []
    s.send_audio = lambda pcm, start, aec: uploaded.append((pcm, start, aec)) or True
    mic = LiveMicrophone(s, on_pcm=lambda pcm, start: recorded.append((pcm, start)))
    # None of the device callbacks run while the session is disconnected.
    mic.feed(b"p" * 3200, end_ms=1100)
    now[0] = 1500
    s._set_state(Conn.DISCONNECTED)
    now[0] = 1900
    s._set_state(Conn.CONNECTED)
    mic.feed(b"a" * 12800, end_ms=1500)  # completely outside the current connection
    mic.feed(b"b" * 12800 + b"c" * 3200, end_ms=2000)
    assert not uploaded
    mic.feed(b"d" * 12800, end_ms=2400)
    assert uploaded == [(b"c" * 3200 + b"d" * 12800, 1900, "off")]
    assert [(pcm[:1], len(pcm), t) for pcm, t in recorded] == [
        (b"p", 3200, 1000), (b"a", 12800, 1100), (b"b", 12800, 1500), (b"c", 16000, 1900)]
    assert bytes(mic.recording) == uploaded[0][0]


def test_qt_does_not_feed_offline_samples_into_the_live_echo_filter(monkeypatch):
    from collections import deque
    from types import SimpleNamespace
    from encore_client.sensing import live_mic

    now = [1000]
    s = Session("ws://test", "ses-reconnect-aec", clock=lambda: now[0])
    s._set_state(Conn.CONNECTED)
    s.set_sensor("microphone", True)
    s.set_sensor("camera", True)
    now[0] = 1500
    s._set_state(Conn.DISCONNECTED)
    now[0] = 1900
    s._set_state(Conn.CONNECTED)
    processed, recorded, uploaded = [], [], []
    s.send_audio = lambda pcm, start, aec: uploaded.append((pcm, start, aec)) or True

    def cancel_echo(pcm, start):
        processed.append((pcm, start))
        return bytes(len(pcm)), "active"

    mic = live_mic.LiveMicrophone(s, clock=lambda: now[0], player=SimpleNamespace(cancel_echo=cancel_echo),
                                  on_pcm=lambda pcm, start: recorded.append((pcm, start)))
    convert = SimpleNamespace(convert=lambda data, **kw: data[::3], delay_ms=0)
    monkeypatch.setattr(live_mic, "AudioConverter", lambda fmt: convert)
    capture = object.__new__(live_mic.QtAudioInput)
    capture.mic = mic
    capture.format = SimpleNamespace(bytesPerFrame=lambda: 2, sampleRate=lambda: 48000)
    reads = iter([b"o" * 38400 + b"n" * 9600, b"v" * 38400])
    capture._io = SimpleNamespace(readAll=lambda: SimpleNamespace(data=lambda: next(reads)))
    capture._capture_start_ms, capture._device_bytes, capture._generation = None, 0, 1
    capture._capture_buffer, capture._resampled_states = bytearray(), deque()
    capture.converter = SimpleNamespace(convert=bytes, delay_ms=0)
    capture._raw_converter = capture._clean_converter = convert
    now[0] = 2000
    capture._read()
    now[0] = 2400
    capture._read()
    assert processed == [(b"n" * 9600, 1900), (b"v" * 38400, 2000)]
    assert recorded == [(b"o" * 12800, 1500), (b"n" * 3200 + b"v" * 12800, 1900)]
    assert uploaded == [(bytes(16000), 1900, "active")]
